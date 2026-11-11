#!/usr/bin/env python3
"""The merger (Phase 9): merges a fix PR on its own, but only for a fix class that measured results have promoted to L2/L3.

    python agent/merger.py run    [--once] [--dry-run] [--interval 30]
    python agent/merger.py status

A separate identity from the fixer: token env EDGELOOP_MERGER_TOKEN (a different GitHub account or fine-grained PAT than
FIXER_GITHUB_TOKEN; Pull requests write on this one repo). The fixer still has no merge call. The merger has no way to
create branches or commits, holds no cluster or model credentials, and never edits code: it can only say yes to a PR that
the fixer already opened and the independent gate already passed. Every decision is written to the audit log.

A PR is merged only when ALL of these hold, re-checked on every pass:
  1. the kill switch is off, no freeze window is open, and the rate limits are not used up
  2. the fix class is promoted to L2/L3 right now (sealed promotions.json, evidence re-validated) and has no standing
     human rejection for this incident signature
  3. the fixer's recorded gate verdict for this incident and class is `pass`, with a risk score small enough for the level
  4. the PR is the fixer's: head branch `sentinel/...`, base main, not a draft, head commit unchanged since first seen
  5. the files really touched (read from GitHub, not from the fixer's own account) stay under allowed prefixes, are few,
     and none is deleted or renamed
  6. when required, GitHub reports every commit signature-verified
  7. no human objection: no CHANGES_REQUESTED review, no `hold` / `do-not-merge` label
  8. the soak delay for the level has passed (time for a human to look)
The merge call pins the head SHA, so a push after the check cannot slip through. After a merge, if the incident is still
open `verify_after_minutes` later, the audit log gets `auto_merge_ineffective`, which demotes the class on the next
`promotion.py apply`. A merge cannot be undone from here: rollback is Flux plus the watchdog (docs/phase5.md).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "policy"))
import controls  # noqa: E402
import feedback as feedback_mod  # noqa: E402
import promotion  # noqa: E402
from github import API, GitHubError, _urllib_transport  # noqa: E402
from incidents import read_events  # noqa: E402

TOKEN_ENV = "EDGELOOP_MERGER_TOKEN"
HOLD_LABELS = {"hold", "do-not-merge", "do not merge"}
ACTOR = "merger"


class MergeClient:
    """Only what the merger needs. Reads, plus one write: merge a PR at a pinned head SHA."""
    def __init__(self, repo, token, transport=None):
        self.repo, self.token, self._t = repo, token, transport or _urllib_transport

    def _req(self, method, path, body=None, ok=(200, 201, 204)):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "Authorization": f"Bearer {self.token}", "User-Agent": "edgeloop-sentinel-merger", "Content-Type": "application/json"}
        status, data = self._t(method, f"{API}{path}", headers, None if body is None else json.dumps(body).encode())
        if status not in ok:
            raise GitHubError(status, (data or {}).get("message", "") if isinstance(data, dict) else str(data))
        return data

    def whoami(self): return self._req("GET", "/user")["login"]
    def repo_info(self): return self._req("GET", f"/repos/{self.repo}")
    def branch(self, name): return self._req("GET", f"/repos/{self.repo}/branches/{name}")
    def pull(self, n): return self._req("GET", f"/repos/{self.repo}/pulls/{n}")
    def files(self, n): return self._req("GET", f"/repos/{self.repo}/pulls/{n}/files?per_page=100")
    def reviews(self, n): return self._req("GET", f"/repos/{self.repo}/pulls/{n}/reviews?per_page=100")
    def commits(self, n): return self._req("GET", f"/repos/{self.repo}/pulls/{n}/commits?per_page=100")

    def merge(self, n, sha):
        """Squash-merge pinned to `sha`: GitHub refuses (409) if the head moved."""
        return self._req("PUT", f"/repos/{self.repo}/pulls/{n}/merge", {"sha": sha, "merge_method": "squash"})


def preflight(gh, fixer_login=None):
    problems = []
    try:
        login = gh.whoami()
    except GitHubError as e:
        return [f"token rejected by GitHub ({e})"]
    if fixer_login and login.lower() == fixer_login.lower():
        problems.append(f"the merger token belongs to {login}, the same account as the fixer: identities must be separate")
    try:
        info = gh.repo_info()
    except GitHubError as e:
        return problems + [f"token cannot read the repository ({e})"]
    if not (info.get("permissions") or {}).get("push"):
        problems.append(f"{login} cannot merge into {gh.repo} (needs write access, Pull requests write)")
    try:
        if not gh.branch("main").get("protected"):
            problems.append("branch 'main' is not protected: enable branch protection first")
    except GitHubError as e:
        problems.append(f"cannot read branch 'main' ({e})")
    return problems


class Merger:
    def __init__(self, gh, *, state_paths, incidents_path, cfg, autonomy, audit, feedback=None, dry_run=False, out=print,
                 levels=None, fixer_login=None):
        self.gh, self.state_paths, self.incidents_path = gh, [Path(p) for p in state_paths], Path(incidents_path)
        self.cfg, self.autonomy, self.audit, self.feedback = cfg, autonomy, audit, feedback
        self.dry_run, self.out, self.levels, self.fixer_login = dry_run, out, levels, fixer_login
        self.last_hold = {}

    # --- inputs --------------------------------------------------------------------
    def _events(self):
        out = []
        for p in self.state_paths:
            out.extend(read_events(p))
        return out

    def open_prs(self):
        ev = self._events()
        prs = {}
        for e in ev:
            if e["event"] == "pr_opened":
                prs[e["pr"]] = dict(e, status="open")
            elif e["event"] in ("pr_merged", "pr_closed") and e.get("pr") in prs:
                prs[e["pr"]]["status"] = e["event"][3:]
        done = {a.get("pr") for a in self.audit.events("auto_merge") if a.get("outcome") == "merged"}
        return [p for p in prs.values() if p["status"] == "open" and p["pr"] not in done], ev

    def _incident(self, iid):
        last = None
        for e in read_events(self.incidents_path):
            if "incident" in e and e["incident"]["id"] == iid:
                last = e["incident"]
        return last

    def current_levels(self):
        if self.levels is not None:
            return self.levels
        lv, problems = promotion.load_levels(self.autonomy)
        for p in problems:
            self.out(f"warn: {p}")
        return lv

    # --- the decision --------------------------------------------------------------
    def evaluate(self, pr, events, now):
        """(level, [reasons it must NOT merge now]). An empty reason list means merge."""
        c = self.cfg
        cls, app, n = pr["playbook"], pr["app"], pr["pr"]
        level = self.current_levels().get(cls, "L1")
        if level not in ("L2", "L3"):
            return level, [f"{cls} is {level}: a human merges"]
        why = controls.pause_reasons(c, self.audit, app, now)
        inc = self._incident(pr["incident"])
        if inc is None or inc.get("state") != "open":
            why.append("the incident is no longer open")
        if self.feedback is not None and inc is not None:
            rej = self.feedback.rejection(feedback_mod.signature(inc), cls)
            if rej:
                why.append("a human rejected this fix class for this signature before")
        gates = [e for e in events if e["event"] == "gate" and e.get("incident") == pr["incident"] and e.get("playbook") == cls]
        g = gates[-1] if gates else None
        cap = self.autonomy["risk"].get("l3_max_score" if level == "L3" else "l2_max_score", 0)
        if not g or g.get("verdict") != "pass":
            why.append("no passing gate verdict is recorded for this PR")
        elif g.get("score") is None or g["score"] > cap:
            why.append(f"risk score {g.get('score')} is above the {level} limit {cap}")
        try:
            info = self.gh.pull(n)
        except GitHubError as e:
            return level, why + [f"cannot read PR: {e}"]
        head = (info.get("head") or {}).get("sha")
        if info.get("state") != "open" or info.get("merged"):
            why.append("PR is not open")
        if info.get("draft"):
            why.append("PR is a draft")
        if not str((info.get("head") or {}).get("ref", "")).startswith("sentinel/") or (info.get("base") or {}).get("ref") != "main":
            why.append("PR is not a sentinel/* branch into main")
        if self.fixer_login and ((info.get("user") or {}).get("login", "").lower() != self.fixer_login.lower()):
            why.append("PR was not opened by the fixer account")
        seen = next((x.get("head") for x in self.audit.events("auto_merge_seen") if x.get("pr") == n), None)
        if seen is None:
            if not self.dry_run:
                self.audit.append(ACTOR, "auto_merge_seen", pr=n, head=head, fix_class=cls, app=app)
        elif seen != head:
            why.append(f"head commit changed after it was first seen ({str(seen)[:8]} -> {str(head)[:8]})")
        try:
            files = self.gh.files(n)
        except GitHubError as e:
            files = None; why.append(f"cannot read PR files: {e}")
        if files is not None:
            pre = tuple(c.get("allowed_path_prefixes") or ["apps/"])
            bad = [f["filename"] for f in files if not f["filename"].startswith(pre) or f.get("status") not in ("modified", "added")]
            if bad:
                why.append("PR touches files outside the allowed set or deletes/renames: " + ", ".join(bad[:3]))
            if len(files) > c.get("max_changed_files", 3):
                why.append(f"PR changes {len(files)} files (limit {c.get('max_changed_files', 3)})")
        if c.get("require_verified_commits", True):
            try:
                cm = self.gh.commits(n)
                if not cm or not all(((x.get("commit") or {}).get("verification") or {}).get("verified") for x in cm):
                    why.append("not every commit in the PR is signature-verified by GitHub")
            except GitHubError as e:
                why.append(f"cannot read commit signatures: {e}")
        labels = {str(l.get("name", "")).lower() for l in info.get("labels") or []}
        if labels & HOLD_LABELS:
            why.append("a human put a hold label on the PR")
        try:
            if any(r.get("state") == "CHANGES_REQUESTED" for r in self.gh.reviews(n)):
                why.append("a reviewer requested changes")
        except GitHubError as e:
            why.append(f"cannot read reviews: {e}")
        soak = 60 * (c.get("soak_minutes") or {}).get(level, 30)
        if now - pr["ts"] < soak:
            why.append(f"soak: {int(soak - (now - pr['ts']))}s left of the {level} delay")
        pr["_head"] = head
        return level, why

    def step(self, now=None):
        now = time.time() if now is None else now
        merged = 0
        prs, events = self.open_prs()
        for pr in prs:
            level, why = self.evaluate(pr, events, now)
            if level not in ("L2", "L3"):
                continue
            n = pr["pr"]
            if why:
                key = "; ".join(why)
                if self.last_hold.get(n) != key:
                    self.last_hold[n] = key
                    self.out(f"hold      PR #{n} ({pr['playbook']}, {level}): {key}")
                    if not self.dry_run:
                        self.audit.append(ACTOR, "auto_merge", pr=n, app=pr["app"], fix_class=pr["playbook"], level=level, outcome="held", reasons=why)
                continue
            if self.dry_run:
                self.out(f"dry-run   would merge PR #{n} ({pr['playbook']}, {level}) at {pr['_head'][:8]}"); continue
            self.audit.append(ACTOR, "auto_merge", pr=n, app=pr["app"], fix_class=pr["playbook"], level=level, outcome="attempt",
                              incident=pr["incident"], head=pr["_head"])          # written BEFORE the call: no unrecorded merge
            try:
                self.gh.merge(n, pr["_head"])
            except GitHubError as e:
                self.audit.append(ACTOR, "auto_merge", pr=n, app=pr["app"], fix_class=pr["playbook"], level=level, outcome="failed", error=str(e)[:200])
                self.out(f"error     PR #{n}: {e}"); continue
            self.audit.append(ACTOR, "auto_merge", pr=n, app=pr["app"], fix_class=pr["playbook"], level=level, outcome="merged",
                              incident=pr["incident"], head=pr["_head"])
            self.out(f"merged    PR #{n} ({pr['playbook']}, {level}) automatically")
            merged += 1
        self.verify_outcomes(now)
        return merged

    def verify_outcomes(self, now):
        """Did an auto-merged fix resolve its incident? A miss is recorded and demotes the class at the next apply."""
        wait = 60 * self.cfg.get("verify_after_minutes", 10)
        judged = {a.get("pr") for a in self.audit.events() if a.get("action") in ("auto_merge_verified", "auto_merge_ineffective")}
        for a in self.audit.events("auto_merge"):
            if a.get("outcome") != "merged" or a["pr"] in judged or now - a["ts"] < wait or self.dry_run:
                continue
            inc = self._incident(a.get("incident"))
            ok = inc is not None and inc.get("state") != "open"
            self.audit.append(ACTOR, "auto_merge_verified" if ok else "auto_merge_ineffective", pr=a["pr"], app=a.get("app"), fix_class=a.get("fix_class"))
            self.out(f"{'verified' if ok else 'INEFFECTIVE':<9} PR #{a['pr']} ({a.get('fix_class')})")
            judged.add(a["pr"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("run", "status"):
        p = sub.add_parser(n)
        p.add_argument("--incidents", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
        p.add_argument("--state", action="append", help="fastpath / fixloop state file (repeatable)")
        p.add_argument("--audit", default=str(controls.AUDIT_FILE))
        p.add_argument("--feedback", default=str(ROOT / "agent" / "state" / "feedback.jsonl"))
        p.add_argument("--repo", help="owner/name (default: parsed from the origin remote)")
        p.add_argument("--fixer-login", default=os.environ.get("EDGELOOP_FIXER_LOGIN"), help="the fixer's GitHub login (merger refuses to be the same account)")
        if n == "run":
            p.add_argument("--interval", type=float, default=30.0)
            p.add_argument("--once", action="store_true")
            p.add_argument("--dry-run", action="store_true", help="evaluate and print, merge nothing, write nothing to the audit log")
    a = ap.parse_args()
    audit = controls.Audit(a.audit)
    if a.cmd == "status":
        lv, problems = promotion.load_levels()
        print("promoted classes:", lv or "none (every class is L1: a human merges)")
        for p in problems:
            print("PROBLEM", p)
        print("pause reasons   :", controls.pause_reasons(controls.load_config(), audit) or "none")
        return 0
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"{TOKEN_ENV} is not set (a merge-only token, separate from {'FIXER_GITHUB_TOKEN'}; see docs/phase9.md)", file=sys.stderr); return 2
    from fastpath import repo_from_origin
    repo = a.repo or repo_from_origin(ROOT)
    gh = MergeClient(repo, token)
    problems = preflight(gh, a.fixer_login)
    if problems:
        print("refusing to start:\n  " + "\n  ".join(problems), file=sys.stderr); return 2
    if os.environ.get("FIXER_GITHUB_TOKEN") == token:
        print("the merger token equals the fixer token: identities must be separate", file=sys.stderr); return 2
    ok, probs = audit.verify()
    if not ok:
        print("refusing to start: the audit log chain is broken:\n  " + "\n  ".join(probs[:5]), file=sys.stderr); return 2
    states = a.state or [str(ROOT / "agent" / "state" / n) for n in ("fastpath.jsonl", "fixloop.jsonl")]
    m = Merger(gh, state_paths=states, incidents_path=a.incidents, cfg=controls.load_config(), autonomy=promotion.load_cfg(), audit=audit,
               feedback=feedback_mod.FeedbackStore(a.feedback), dry_run=a.dry_run, out=lambda s: print(s, flush=True), fixer_login=a.fixer_login)
    print(f"merger started repo={repo} dry_run={a.dry_run}", flush=True)
    while True:
        try:
            m.step()
        except Exception as e:      # a bad pass must not kill the loop, and must never merge
            print(f"pass failed: {e}", flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
