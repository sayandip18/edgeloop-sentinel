#!/usr/bin/env python3
"""Escalation (Phase 8): when no automated path will fix an incident, page a human with a full evidence bundle.

    python agent/escalate.py run  [--once] [--interval 15] [--webhook-url URL] [--no-fixer|--fixer]
    python agent/escalate.py list [--all]          # open escalations (not acknowledged)
    python agent/escalate.py show ESCALATION_ID    # print the evidence bundle
    python agent/escalate.py ack  ESCALATION_ID [--note TEXT]

What counts as "no automated path left" (one escalation per incident and reason, never repeated):
  diagnosis-escalated   the diagnosis agent was not confident enough, errored or ran out of budget
  no-automated-fix      the fast path declined and the LLM fixer cannot or will not act (declined, errored, or the
                        diagnosis is not actionable). Includes "a human rejected this fix class before"
  fix-rejected          a human closed our PR without merging it (also recorded as feedback, see feedback.py)
  fix-ineffective       our PR was merged and the incident is still open afterwards
  pr-stale              our PR has waited for review longer than --pr-stale-s
  unattended            the incident has been open --unattended-s with no PR and no other escalation (the loops may be down)
A diagnosis of "no fault" (the decoys) never escalates by itself: that is the system correctly doing nothing.

Channels: stdout, a bundle on disk (always), EMAIL (the chosen channel) and an optional webhook.
- Email: SMTP, configured only from the environment (the password never touches a flag, a file in the repo or a log):
  EDGELOOP_SMTP_HOST, EDGELOOP_SMTP_PORT (default 587), EDGELOOP_SMTP_USER, EDGELOOP_SMTP_PASSWORD, EDGELOOP_EMAIL_FROM,
  EDGELOOP_EMAIL_TO (comma-separated), EDGELOOP_SMTP_SECURITY = starttls (default) | ssl. TLS with certificate checks is
  mandatory; there is no plaintext mode. The message is plain text only: a sanitised summary, then the redacted evidence
  bundle as text. Subject and addresses are single-line and validated (no header injection).
- Webhook (EDGELOOP_ESCALATION_WEBHOOK or --webhook-url; Slack-compatible `{"text": ...}`): a short sanitised summary and
  the bundle's path, never the bundle.

Safety
- Read-only with respect to the world: it reads the incident log and the loops' state files, and writes only its own files
  (escalations.jsonl, escalations/*.json|md, and the feedback store). No cluster, no GitHub token, no LLM.
- The bundle is built from state files and is run through the same secret/PII redaction as everything the models see.
- Text that came from logs or from a model (root cause, rationale, reasons) is untrusted: it is capped, redacted, and in the
  webhook message it is stripped of @mentions, links and markup, so it cannot ping people or inject a link.
- Paging is best effort and idempotent: a failed webhook is retried up to --max-notify-attempts times, then left visible in
  `list` as un-notified. The bundle on disk exists either way.
"""
import argparse
import json
import os
import re
import sys
import time
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import feedback as feedback_mod  # noqa: E402
from incidents import read_events  # noqa: E402
from redact import redact_obj, redact  # noqa: E402

STATE = ROOT / "agent" / "state"
WEBHOOK_ENV = "EDGELOOP_ESCALATION_WEBHOOK"
MAX_RETRIES = 3          # the fast path's own retry ceiling (fastpath.MAX_RETRIES): errors at/over it mean it gave up

NEXT_STEPS = {
    "diagnosis-escalated": ["Read the diagnosis summary and tool calls in the bundle; the model was not confident enough to act.",
                            "Check the evidence yourself (kubectl describe / logs); fix by hand or open a PR."],
    "no-automated-fix": ["No playbook applied and the LLM fixer declined or could not produce a safe patch (see 'automation').",
                         "Fix by hand through a PR (Git is the only write path). Rolling back the last change to the app is a good first move."],
    "fix-rejected": ["A human closed our PR. The fix class is now blocked for this signature (python agent/feedback.py list).",
                     "Fix the incident by hand. If the rejection was a mistake, clear it: python agent/feedback.py forget SIGNATURE FIX_CLASS."],
    "fix-ineffective": ["Our fix was merged but the incident is still open: the diagnosis or the patch was wrong.",
                        "Consider reverting the merged PR (the host watchdog may already have), then fix by hand."],
    "pr-stale": ["The fix PR is waiting for a human. Review and merge it, or close it (closing teaches the system not to propose it again)."],
    "unattended": ["The incident has been open a long time with no automated activity: check that watch.py, diagnose_loop.py and the "
                   "fixer loops are running."],
}


# --- helpers ---------------------------------------------------------------------------
def _cap(s, n):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def chat_safe(s, n=200):
    """Untrusted text for a chat message: redacted, one line, capped, and with everything a chat app would interpret
    (@mentions, <links>, markup, backticks, URLs) neutralised."""
    s = redact(_cap(s, n * 2))[0]
    s = re.sub(r"https?://\S+", "[link removed]", s)
    s = re.sub(r"[@<>&`*_~|\\]", "", s)
    return _cap(s, n)


def latest_incidents(path):
    last = {}
    for ev in read_events(path):
        if "incident" in ev:
            last[ev["incident"]["id"]] = ev["incident"]
    return last


def latest_diagnoses(path):
    """incident id -> (latest `diagnosed` event, number of attempts)."""
    out, n = {}, {}
    for ev in read_events(path):
        if ev.get("event") == "diagnosed" and ev.get("incident_id"):
            out[ev["incident_id"]] = ev
            n[ev["incident_id"]] = n.get(ev["incident_id"], 0) + 1
    return {k: (v, n[k]) for k, v in out.items()}


def _post_webhook(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "edgeloop-sentinel-escalator"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


ADDR = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def email_config_from_env(env=os.environ):
    """(config, problem). config is None when email is not configured at all; a half-configured setup is a problem."""
    keys = ("EDGELOOP_SMTP_HOST", "EDGELOOP_EMAIL_FROM", "EDGELOOP_EMAIL_TO")
    if not any(env.get(k) for k in keys + ("EDGELOOP_SMTP_USER", "EDGELOOP_SMTP_PASSWORD")):
        return None, None
    missing = [k for k in keys if not env.get(k)]
    if missing:
        return None, f"email is half-configured; also set {', '.join(missing)}"
    to = [t.strip() for t in env["EDGELOOP_EMAIL_TO"].split(",") if t.strip()]
    sec = (env.get("EDGELOOP_SMTP_SECURITY") or "starttls").lower()
    if sec not in ("starttls", "ssl"):
        return None, "EDGELOOP_SMTP_SECURITY must be starttls or ssl (there is no plaintext mode)"
    if not ADDR.match(env["EDGELOOP_EMAIL_FROM"]) or not to or not all(ADDR.match(t) for t in to):
        return None, "EDGELOOP_EMAIL_FROM / EDGELOOP_EMAIL_TO must be plain single addresses (comma-separated for TO)"
    if bool(env.get("EDGELOOP_SMTP_USER")) != bool(env.get("EDGELOOP_SMTP_PASSWORD")):
        return None, "set both EDGELOOP_SMTP_USER and EDGELOOP_SMTP_PASSWORD, or neither"
    try:
        port = int(env.get("EDGELOOP_SMTP_PORT") or (465 if sec == "ssl" else 587))
    except ValueError:
        return None, "EDGELOOP_SMTP_PORT must be a number"
    return {"host": env["EDGELOOP_SMTP_HOST"], "port": port, "user": env.get("EDGELOOP_SMTP_USER"),
            "password": env.get("EDGELOOP_SMTP_PASSWORD"), "from": env["EDGELOOP_EMAIL_FROM"], "to": to, "security": sec}, None


def _send_mail(cfg, msg):
    ctx = ssl.create_default_context()          # certificate and hostname verification on
    if cfg["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=ctx)
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
    with smtp:
        if cfg["security"] == "starttls":
            smtp.starttls(context=ctx)
        if cfg["user"]:
            smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def webhook_url_problem(url):
    if not re.match(r"^https://[^/\s]+", url) and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", url):
        return "the webhook URL must be https:// (http:// only for localhost)"
    return None


# --- the escalator --------------------------------------------------------------------------
class Escalator:
    def __init__(self, *, incidents_path, fastpath_path=None, fixloop_path=None, out_dir=STATE / "escalations", log_path=None,
                 feedback=None, repo=None, fixer_active=None, webhook_url=None, post=None, email=None, send_mail=None, out=print,
                 pr_stale_s=4 * 3600, unattended_s=1800, ineffective_after_s=600, max_notify_attempts=3):
        self.incidents_path = incidents_path
        self.fp_path, self.fl_path = fastpath_path, fixloop_path
        self.out_dir = Path(out_dir)
        self.log_path = Path(log_path) if log_path else self.out_dir.parent / "escalations.jsonl"
        self.feedback, self.repo, self.out = feedback, repo, out
        self.webhook_url, self.post = webhook_url, post or _post_webhook
        self.email, self.send_mail = email, send_mail or _send_mail
        self.pr_stale_s, self.unattended_s, self.ineffective_after_s = pr_stale_s, unattended_s, ineffective_after_s
        self.max_notify_attempts = max_notify_attempts
        self.fixer_active = fixer_active          # None: auto (the fix loop's state file exists)

    # -- state ------------------------------------------------------------------------
    def _events(self, path):
        return read_events(path) if path else []

    def _fixer_on(self):
        return self.fixer_active if self.fixer_active is not None else bool(self.fl_path and Path(self.fl_path).exists())

    def _log(self):
        return read_events(self.log_path)

    def _append(self, event, **kw):
        ev = {"ts": time.time(), "event": event, **kw}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        return ev

    # -- reasons ------------------------------------------------------------------------
    def reasons(self, inc, diag, fp, fl, now):
        """Reasons to escalate this open incident now: list of (reason, detail). Pure function of the state files."""
        iid, found = inc["id"], []
        mine = lambda evs: [e for e in evs if e.get("incident") == iid]
        fp, fl = mine(fp), mine(fl)
        prs = {}
        for e in fp + fl:
            if e["event"] == "pr_opened":
                prs[e["pr"]] = dict(e, status="open")
        for e in fp + fl:
            if e["event"] in ("pr_merged", "pr_closed") and e.get("pr") in prs:
                prs[e["pr"]].update(status=e["event"][3:], outcome_ts=e["ts"])
        d, n_diag = diag if diag else (None, 0)
        none_diag = bool(d and d.get("action") == "diagnosed" and d.get("diagnosis")
                         and d["diagnosis"]["hypotheses"][0]["category"] == "none")

        # a human acted on our PR
        for p in prs.values():
            if p["status"] == "closed":
                found.append(("fix-rejected", f"PR #{p['pr']} ({p['playbook']}) was closed without merging"))
            elif p["status"] == "merged" and now - p["outcome_ts"] >= self.ineffective_after_s:
                found.append(("fix-ineffective", f"PR #{p['pr']} ({p['playbook']}) was merged {int(now - p['outcome_ts'])}s ago and the incident is still open"))
            elif p["status"] == "open" and now - p["ts"] >= self.pr_stale_s:
                found.append(("pr-stale", f"PR #{p['pr']} has waited {int((now - p['ts']) // 60)} min for review"))

        # the diagnosis gave up (a retryable API failure is not final until its attempts are used up)
        if d and d.get("action") == "escalate" and ("LLM call failed" not in (d.get("reason") or "") or n_diag >= 2):
            found.append(("diagnosis-escalated", d.get("reason") or "diagnosis escalated"))

        # nothing automated is left and no PR exists
        fp_on = bool(self.fp_path and Path(self.fp_path).exists())
        diag_gave_up = any(r == "diagnosis-escalated" for r, _ in found)      # that page already says a human is needed
        if not prs and not none_diag and not diag_gave_up and (fp_on or self._fixer_on()):
            fp_gave_up = (not fp_on) or any(e["event"] == "declined" for e in fp) or \
                sum(e["event"] == "error" for e in fp) >= MAX_RETRIES
            if self._fixer_on():
                fl_gave_up = any(e["event"] == "declined" for e in fl) or sum(e["event"] == "error" for e in fl) >= MAX_RETRIES \
                    or bool(d and not self._actionable(inc, d))
            else:
                fl_gave_up = True
            if fp_gave_up and fl_gave_up:
                found.append(("no-automated-fix", self._why_no_fix(fp, fl, d)))

        # safety net: something is wrong with the loops themselves
        if not prs and not none_diag and not found and now - inc["opened_at"] >= self.unattended_s:
            found.append(("unattended", f"open {int((now - inc['opened_at']) // 60)} min with no PR and no escalation"))
        return found

    @staticmethod
    def _actionable(inc, d):
        from fix_loop import FixLoop     # the fixer's own rule, not a copy of it
        return FixLoop._actionable(inc, d)

    @staticmethod
    def _why_no_fix(fp, fl, d):
        bits = []
        for name, evs in (("fast path", fp), ("LLM fixer", fl)):
            dec = [e for e in evs if e["event"] == "declined"]
            if dec:
                bits.append(f"{name} declined: {_cap(dec[-1].get('reason'), 240)}")
        if d and d.get("action") == "diagnosed" and not bits:
            bits.append("the diagnosis is not an actionable fault with a fixable class")
        return "; ".join(bits) or "no automated path produced a fix"

    # -- bundle -------------------------------------------------------------------------
    def bundle(self, inc, reason, detail, diag, fp, fl, now):
        iid = inc["id"]
        mine = lambda evs: [e for e in evs if e.get("incident") == iid]
        sig = feedback_mod.signature(inc)
        dd, n_diag = diag if diag else (None, 0)
        diagnosis = None
        if dd:
            diagnosis = {"action": dd.get("action"), "reason": dd.get("reason"), "model": dd.get("model"), "attempts": n_diag,
                         "tokens": dd.get("tokens"), "result": dd.get("diagnosis"),
                         "tool_calls": [{"name": t.get("name"), "args": t.get("args") or t.get("arguments")} for t in (dd.get("tool_calls") or [])][:20]}
        auto = {}
        for name, evs in (("fastpath", mine(fp)), ("fixloop", mine(fl))):
            auto[name] = [{k: e.get(k) for k in ("ts", "event", "reason", "status", "attempt", "verdict", "level", "failed", "playbook", "pr", "url", "score", "attempt")
                           if e.get(k) is not None} for e in evs if e["event"] not in ("started",)]
        rej = [{"fix_class": e["fix_class"], "url": e.get("url"), "via": e.get("via"), "reason": e.get("reason")}
               for (s, _), e in (self.feedback.decisions().items() if self.feedback else []) if s == sig and e["event"] == "rejected"]
        commits = []
        if self.repo is not None:
            try:
                commits = [{"sha": c.sha[:10], "when": datetime.fromtimestamp(c.ts, timezone.utc).isoformat(timespec="seconds"), "subject": c.subject}
                           for c in self.repo.log(f"apps/{inc['app']}", 5)]
            except Exception:
                commits = []
        b = {"escalation_id": f"esc-{iid}-{reason}", "reason": reason, "detail": detail, "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
             "signature": sig, "incident": inc, "diagnosis": diagnosis, "automation": auto, "standing_rejections": rej,
             "recent_commits": commits, "next_steps": NEXT_STEPS[reason]}
        clean, counts = redact_obj(b)
        clean["redactions"] = counts
        return clean

    @staticmethod
    def markdown(b):
        inc, dg = b["incident"], b["diagnosis"]
        L = [f"# Escalation: {b['reason']} on {inc['app']}", "",
             f"- escalation: `{b['escalation_id']}`  (created {b['created_at']})",
             f"- incident: `{inc['id']}`, state {inc['state']}, signature `{b['signature']}`",
             f"- why a human is needed: {b['detail']}", "", "## Signals (from the detector)"]
        L += [f"- {s['type']} on {s['app']} (x{s.get('count', 1)}): {_cap(s['detail'], 200)}" for s in inc["signals"]]
        if inc.get("affected_apps"):
            L.append(f"- also affecting: {', '.join(inc['affected_apps'])}")
        L += ["", "## Diagnosis (model text, untrusted)"]
        if not dg:
            L.append("- none recorded")
        else:
            L.append(f"- action {dg['action']}; {_cap(dg['reason'], 200)}; model {dg['model']}; {dg['attempts']} attempt(s)")
            r = dg.get("result") or {}
            for h in r.get("hypotheses", []):
                L.append(f"- hypothesis: {h['category']} on {h['target_app']} (confidence {h['confidence']:.2f}): {_cap(h['root_cause'], 300)}")
            if r:
                L.append(f"- remediation class: {r.get('remediation_class')}; summary: {_cap(r.get('summary'), 300)}")
        L += ["", "## What the automation did"]
        for name, evs in b["automation"].items():
            L.append(f"- {name}: " + ("; ".join(f"{e['event']}" + (f" ({_cap(e.get('reason'), 160)})" if e.get("reason") else "")
                                                + (f" {e['url']}" if e.get("url") else "") for e in evs) or "nothing recorded"))
        if b["standing_rejections"]:
            L += ["", "## Fixes a human rejected earlier for this signature"]
            L += [f"- {r['fix_class']} ({r.get('url') or r.get('via')})" for r in b["standing_rejections"]]
        if b["recent_commits"]:
            L += ["", "## Recent commits touching the app"] + [f"- {c['sha']} {c['when']} {_cap(c['subject'], 100)}" for c in b["recent_commits"]]
        L += ["", "## What to do"] + [f"- {s}" for s in b["next_steps"]]
        return "\n".join(L) + "\n"

    # -- notification ---------------------------------------------------------------------
    def message(self, b):
        inc = b["incident"]
        prs = [e.get("url") for evs in b["automation"].values() for e in evs if e.get("url")]
        return (f"[edgeloop-sentinel] Human needed: {b['reason']} on {chat_safe(inc['app'], 60)}\n"
                f"incident {chat_safe(inc['id'], 80)} (signature {chat_safe(b['signature'], 80)})\n"
                f"why: {chat_safe(b['detail'], 240)}\n"
                + (f"PR: {chat_safe(prs[-1], 120)}\n" if prs and re.match(r"https://github\.com/[\w.\-]+/[\w.\-]+/pull/\d+$", prs[-1]) else "")
                + f"evidence bundle: {b['_path']}")

    def channels(self):
        out = []
        if self.email:
            out.append("email")
        if self.webhook_url:
            out.append("webhook")
        return out

    def email_message(self, b):
        inc = b["incident"]
        m = EmailMessage()
        m["From"], m["To"] = self.email["from"], ", ".join(self.email["to"])
        m["Subject"] = f"[edgeloop-sentinel] {b['reason']} on {chat_safe(inc['app'], 60)} ({chat_safe(inc['id'], 60)})"
        m.set_content(self.message(b) + "\n\n" + "-" * 60 + "\n\n" + self.markdown(b))
        return m

    def _send(self, channel, b):
        if channel == "webhook":
            status = self.post(self.webhook_url, {"text": self.message(b)})
            if not 200 <= int(status) < 300:
                raise ValueError(f"HTTP {status}")
        else:
            self.send_mail(self.email, self.email_message(b))

    def notify(self, esc_id, b):
        """Page on every configured channel. The bundle on disk is the durable record either way. Each channel has its own
        bounded retry budget, so a dead webhook does not stop the email (or the other way round)."""
        log = [e for e in self._log() if e.get("escalation_id") == esc_id]
        for ch in self.channels():
            if any(e["event"] == "notified" and e.get("channel") == ch for e in log):
                continue
            tries = sum(e["event"] == "notify_failed" and e.get("channel") == ch for e in log)
            if tries >= self.max_notify_attempts:
                continue
            try:
                self._send(ch, b)
                err = None
            except (urllib.error.URLError, smtplib.SMTPException, ssl.SSLError, OSError, ValueError) as e:
                err = type(e).__name__          # never log the exception text: it can contain the URL or an address
            if err is None:
                self._append("notified", escalation_id=esc_id, channel=ch)
                self.out(f"paged     {esc_id} via {ch}")
            else:
                self._append("notify_failed", escalation_id=esc_id, channel=ch, error=err, attempt=tries + 1)
                self.out(f"warn: {ch} for {esc_id} failed ({err}); attempt {tries + 1}/{self.max_notify_attempts}")

    # -- one pass ---------------------------------------------------------------------------
    def step(self, now=None):
        now = time.time() if now is None else now
        incs = latest_incidents(self.incidents_path)
        fp, fl = self._events(self.fp_path), self._events(self.fl_path)
        if self.feedback is not None:
            self.feedback.ingest(incs, [fp, fl])
        diags = latest_diagnoses(self.incidents_path)
        done = {e["escalation_id"] for e in self._log() if e["event"] == "escalated"}
        new = []
        for inc in sorted(incs.values(), key=lambda i: i["opened_at"]):
            if inc["state"] != "open":
                continue
            for reason, detail in self.reasons(inc, diags.get(inc["id"]), fp, fl, now):
                eid = f"esc-{inc['id']}-{reason}"
                if eid in done:
                    continue
                b = self.bundle(inc, reason, detail, diags.get(inc["id"]), fp, fl, now)
                self.out_dir.mkdir(parents=True, exist_ok=True)
                path = self.out_dir / f"{eid}.json"
                path.write_text(json.dumps(b, indent=2), encoding="utf-8")
                (self.out_dir / f"{eid}.md").write_text(self.markdown(b), encoding="utf-8")
                b["_path"] = str(path)
                self._append("escalated", escalation_id=eid, incident=inc["id"], app=inc["app"], reason=reason, detail=_cap(redact(detail)[0], 300),
                             bundle=str(path), signature=b["signature"])
                self.out(f"ESCALATE  {inc['id']} ({inc['app']}): {reason}: {_cap(detail, 200)}\n          bundle: {path}")
                done.add(eid)
                new.append(eid)
        # (re)try paging for anything not yet delivered, including from earlier passes
        if self.channels():
            for e in self._log():
                if e["event"] == "escalated":
                    p = Path(e["bundle"])
                    if p.exists():
                        b = json.loads(p.read_text(encoding="utf-8")); b["_path"] = str(p)
                        self.notify(e["escalation_id"], b)
        return new

    def open_escalations(self, include_acked=False):
        log = self._log()
        acked = {e["escalation_id"] for e in log if e["event"] == "acked"}
        notified = {e["escalation_id"] for e in log if e["event"] == "notified"}
        return [dict(e, acked=e["escalation_id"] in acked, notified=e["escalation_id"] in notified) for e in log
                if e["event"] == "escalated" and (include_acked or e["escalation_id"] not in acked)]

    def ack(self, esc_id, note=""):
        if not any(e["event"] == "escalated" and e["escalation_id"] == esc_id for e in self._log()):
            raise KeyError(esc_id)
        return self._append("acked", escalation_id=esc_id, note=_cap(redact(note)[0], 300))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--incidents", default=str(STATE / "incidents.jsonl"))
    common.add_argument("--fastpath-state", default=str(STATE / "fastpath.jsonl"))
    common.add_argument("--fixloop-state", default=str(STATE / "fixloop.jsonl"))
    common.add_argument("--out-dir", default=str(STATE / "escalations"))
    common.add_argument("--feedback", default=str(STATE / "feedback.jsonl"))
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--interval", type=float, default=15.0)
    r.add_argument("--once", action="store_true")
    r.add_argument("--duration", type=float, default=0)
    r.add_argument("--webhook-url", help=f"default: env {WEBHOOK_ENV} (https only)")
    r.add_argument("--fixer", dest="fixer", action="store_true", default=None, help="the LLM fix loop is part of this deployment")
    r.add_argument("--no-fixer", dest="fixer", action="store_false", help="it is not (default: auto-detect from its state file)")
    r.add_argument("--pr-stale-s", type=float, default=4 * 3600)
    r.add_argument("--unattended-s", type=float, default=1800)
    r.add_argument("--ineffective-after-s", type=float, default=600)
    r.add_argument("--max-notify-attempts", type=int, default=3)
    for name in ("list", "show", "ack"):
        p = sub.add_parser(name, parents=[common])
        if name == "list":
            p.add_argument("--all", action="store_true")
        else:
            p.add_argument("escalation_id")
        if name == "ack":
            p.add_argument("--note", default="")
    a = ap.parse_args()

    def make(**kw):
        from gitrepo import GitRepo
        return Escalator(incidents_path=a.incidents, fastpath_path=a.fastpath_state, fixloop_path=a.fixloop_state, out_dir=a.out_dir,
                         feedback=feedback_mod.FeedbackStore(a.feedback), repo=GitRepo(ROOT), **kw)

    if a.cmd == "list":
        rows = make().open_escalations(include_acked=a.all)
        for e in rows:
            print(f"{e['escalation_id']:<54} {e['app']:<14} {e['reason']:<22} {'acked' if e['acked'] else 'OPEN '} "
                  f"{'paged' if e['notified'] else 'not-paged'}  {e['detail'][:80]}")
        print(f"{len(rows)} escalation(s)")
        return 0
    if a.cmd == "show":
        p = Path(a.out_dir) / f"{re.sub(r'[^A-Za-z0-9_.-]', '', a.escalation_id)}.md"
        if not p.is_file():
            print(f"no bundle {p}", file=sys.stderr); return 2
        print(p.read_text(encoding="utf-8")); return 0
    if a.cmd == "ack":
        try:
            make().ack(a.escalation_id, a.note)
        except KeyError:
            print(f"no escalation {a.escalation_id}", file=sys.stderr); return 2
        print(f"acknowledged {a.escalation_id}"); return 0

    url = a.webhook_url or os.environ.get(WEBHOOK_ENV)
    if url and webhook_url_problem(url):
        print(webhook_url_problem(url), file=sys.stderr); return 2
    email, eprob = email_config_from_env()
    if eprob:
        print(eprob, file=sys.stderr); return 2
    esc = make(fixer_active=a.fixer, webhook_url=url, email=email, pr_stale_s=a.pr_stale_s, unattended_s=a.unattended_s,
               ineffective_after_s=a.ineffective_after_s, max_notify_attempts=a.max_notify_attempts, out=lambda m: print(m, flush=True))
    print(f"escalate started incidents={a.incidents} email={'to ' + str(len(email['to'])) + ' recipient(s)' if email else 'off'} webhook={'on' if url else 'off'} (bundles are always written to disk)", flush=True)
    t0 = time.time()
    while True:
        try:
            esc.step()
        except Exception as e:
            print(f"warn: pass failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if a.once or (a.duration and time.time() - t0 >= a.duration):
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
