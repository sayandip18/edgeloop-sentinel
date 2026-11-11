#!/usr/bin/env python3
"""Hardening controls (Phase 9): global kill switch, change-freeze windows, rate limits and a tamper-evident audit log.

    python policy/controls.py status
    python policy/controls.py kill   --reason "why"      # stop every automated change now (creates agent/state/KILL)
    python policy/controls.py resume --reason "why"
    python policy/controls.py audit-verify               # hash chain intact?
    python policy/controls.py audit-tail [-n 20]

Everything here is deterministic and needs no cluster, token or model.

Kill switch: the file `agent/state/KILL` (or env EDGELOOP_KILL=1). While it exists the fast path, the LLM fix loop and
the merger do nothing. The escalator keeps paging humans: a stopped system must still be able to say so.
Freeze windows and rate limits come from policy/autonomy.yaml (`controls:`). They stop automatic *merging*.
Audit log: append-only JSONL, each record carries the SHA-256 of the previous record, so editing, deleting or
reordering any line breaks the chain and `audit-verify` says where. This is tamper-EVIDENT, not tamper-proof: someone
who can rewrite the whole file can rewrite the chain too. Anchor the head hash somewhere the agent cannot write
(`audit-head`, e.g. paste it into a commit message or an email) and use OS-level append-only (chattr +a) where available.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / "agent" / "state"
KILL_FILE = STATE / "KILL"
AUDIT_FILE = STATE / "audit.jsonl"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
GENESIS = "0" * 64


def load_config(path=None):
    cfg = yaml.safe_load(Path(path or HERE / "autonomy.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("controls") or {}


# --- audit log -----------------------------------------------------------------------------------------------------------
def _digest(prev, rec):
    body = json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


class Audit:
    def __init__(self, path=AUDIT_FILE):
        self.path = Path(path) if path else None
        self.mem = []
        if self.path and self.path.exists():
            self.mem = self._read()

    def _read(self):
        out = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"_corrupt_line": i})
        return out

    def refresh(self):
        if self.path and self.path.exists():
            self.mem = self._read()

    def head(self):
        return self.mem[-1].get("hash", GENESIS) if self.mem and "hash" in self.mem[-1] else GENESIS

    def append(self, actor, action, **detail):
        self.refresh()                    # another process may have appended since we last looked
        rec = {"seq": len(self.mem) + 1, "ts": round(time.time(), 3), "actor": actor, "action": action, **detail, "prev": self.head()}
        rec["hash"] = _digest(rec["prev"], rec)
        self.mem.append(rec)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        return rec

    def verify(self):
        """(ok, problems). Checks sequence numbers, the previous-hash links and each record's own hash."""
        self.refresh()
        problems, prev = [], GENESIS
        for i, rec in enumerate(self.mem, 1):
            if "_corrupt_line" in rec:
                problems.append(f"line {rec['_corrupt_line']}: not valid JSON"); continue
            if rec.get("seq") != i:
                problems.append(f"record {i}: sequence is {rec.get('seq')} (a record was removed, added or reordered)")
            if rec.get("prev") != prev:
                problems.append(f"record {i}: previous-hash link broken")
            if rec.get("hash") != _digest(rec.get("prev", ""), rec):
                problems.append(f"record {i}: content does not match its hash (edited)")
            prev = rec.get("hash", "")
        return not problems, problems

    def events(self, action=None):
        return [e for e in self.mem if action is None or e.get("action") == action]


# --- controls --------------------------------------------------------------------------------------------------------------
def killed(kill_file=None):
    """The global kill switch. Cheap: called on every loop pass."""
    return os.environ.get("EDGELOOP_KILL", "") not in ("", "0") or Path(kill_file or KILL_FILE).exists()


def kill_reason(kill_file=None):
    if os.environ.get("EDGELOOP_KILL", "") not in ("", "0"):
        return "EDGELOOP_KILL is set"
    f = Path(kill_file or KILL_FILE)
    try:
        return f.read_text(encoding="utf-8").strip().splitlines()[0] if f.exists() else ""
    except (OSError, IndexError):
        return "kill file present"


def _parse(ts):
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def freeze_reason(cfg, now=None):
    """Why changes are frozen right now, or None. Windows: {start, end, reason} (ISO, UTC unless an offset is given) and
    weekly rules {days: [fri, sat], from: "17:00", to: "23:59", reason} (UTC)."""
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now, timezone.utc)
    for w in cfg.get("freeze_windows") or []:
        try:
            if _parse(w["start"]) <= dt < _parse(w["end"]):
                return w.get("reason") or f"change freeze until {w['end']}"
        except (KeyError, ValueError):
            return "a freeze window in policy/autonomy.yaml is malformed: failing closed"
    for w in cfg.get("weekly_freeze") or []:
        try:
            days = [str(d).lower()[:3] for d in w["days"]]
            hhmm = dt.strftime("%H:%M")
            if DAYS[dt.weekday()] in days and str(w.get("from", "00:00")) <= hhmm <= str(w.get("to", "23:59")):
                return w.get("reason") or "weekly change freeze"
        except (KeyError, TypeError):
            return "a weekly freeze rule in policy/autonomy.yaml is malformed: failing closed"
    return None


def rate_problem(cfg, audit, app=None, now=None):
    """Auto-merge rate limits, counted from the audit log (so they survive restarts). None when within limits."""
    now = time.time() if now is None else now
    merges = [e for e in audit.events("auto_merge") if e.get("outcome") == "merged"]
    lim = cfg.get("rate_limits") or {}
    for key, window, label in (("max_merges_per_hour", 3600, "hour"), ("max_merges_per_day", 86400, "24h")):
        cap = lim.get(key)
        if cap is not None and sum(1 for e in merges if now - e["ts"] < window) >= cap:
            return f"rate limit: {cap} auto-merge(s) already happened in the last {label}"
    cap = lim.get("max_merges_per_app_per_day")
    if app and cap is not None and sum(1 for e in merges if e.get("app") == app and now - e["ts"] < 86400) >= cap:
        return f"rate limit: {cap} auto-merge(s) of {app} already in the last 24h"
    return None


def pause_reasons(cfg, audit, app=None, now=None):
    """Everything that stops an automatic merge right now (empty list = clear)."""
    out = []
    if killed():
        out.append(f"kill switch: {kill_reason() or 'active'}")
    fr = freeze_reason(cfg, now)
    if fr:
        out.append(f"change freeze: {fr}")
    rp = rate_problem(cfg, audit, app, now)
    if rp:
        out.append(rp)
    return out


# --- CLI -----------------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("status", "kill", "resume", "audit-verify", "audit-tail", "audit-head"):
        p = sub.add_parser(n)
        p.add_argument("--audit", default=str(AUDIT_FILE))
        p.add_argument("--kill-file", default=str(KILL_FILE))
        if n in ("kill", "resume"):
            p.add_argument("--reason", required=True)
        if n == "audit-tail":
            p.add_argument("-n", type=int, default=20)
    a = ap.parse_args()
    audit = Audit(a.audit)
    kf = Path(a.kill_file)
    if a.cmd == "kill":
        kf.parent.mkdir(parents=True, exist_ok=True)
        kf.write_text(a.reason.strip()[:300] + "\n", encoding="utf-8")
        audit.append("human", "kill_switch_on", reason=a.reason[:300])
        print("KILL SWITCH ON: the fast path, the fix loop and the merger will stop on their next pass")
    elif a.cmd == "resume":
        if kf.exists():
            kf.unlink()
        audit.append("human", "kill_switch_off", reason=a.reason[:300])
        print("kill switch off")
    elif a.cmd == "status":
        cfg = load_config()
        print(f"kill switch : {'ON (' + (kill_reason(kf) or 'active') + ')' if killed(kf) else 'off'}")
        print(f"freeze      : {freeze_reason(cfg) or 'none right now'}")
        print(f"rate limits : {rate_problem(cfg, audit) or 'within limits'}  ({cfg.get('rate_limits')})")
        ok, probs = audit.verify()
        print(f"audit log   : {len(audit.mem)} record(s), chain {'intact' if ok else 'BROKEN'}; head {audit.head()[:16]}")
        for p in probs[:5]:
            print("   ", p)
        return 0 if ok else 1
    elif a.cmd == "audit-verify":
        ok, probs = audit.verify()
        print(f"{len(audit.mem)} record(s): " + ("chain intact" if ok else "CHAIN BROKEN"))
        for p in probs:
            print("  ", p)
        return 0 if ok else 1
    elif a.cmd == "audit-head":
        print(f"{len(audit.mem)} {audit.head()}")
    elif a.cmd == "audit-tail":
        for r in audit.mem[-a.n:]:
            print(json.dumps({k: v for k, v in r.items() if k not in ("prev", "hash")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
