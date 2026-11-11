#!/usr/bin/env python3
"""Learning loop (Phase 8): human decisions on our pull requests are remembered, so a fix a human rejected is not
proposed again for the same incident signature.

    python agent/feedback.py list   [--feedback PATH]
    python agent/feedback.py ingest [--incidents PATH] [--state PATH ...]     # fold PR outcomes into the store
    python agent/feedback.py reject SIGNATURE FIX_CLASS [--reason TEXT]        # a rejection you did not do through a PR
    python agent/feedback.py forget SIGNATURE FIX_CLASS                        # allow that fix to be proposed again

Vocabulary
- signature: `<root app>:<primary signal type>` of an incident, e.g. `oom-app:oom_killed`. Coarse on purpose: the same
  failure on the same app must match even when a second signal (crash loop, restarts) joins the incident a bit later.
- fix class: the playbook or LLM fix class that wrote the PR (`revert-recent-change`, `llm-fix-env-var`, ...). Classes are
  compared by *family*, so a human who rejects the `revert-recent-change` playbook also blocks `llm-revert-change`:
  they are the same kind of fix, and re-proposing it through the other door would defeat the point.

Decisions come only from GitHub's own record of what happened to a PR, as written into the loops' state files:
`pr_closed` (closed without merging) is a rejection, `pr_merged` an approval. Nothing in the text of a PR, an incident or
a log can create, change or clear a decision. The store is append-only JSONL, the latest decision per
(signature, fix family) wins, and `forget` is a deliberate human action.

This module needs no token and no cluster: it reads state files and appends to its own file.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from incidents import read_events  # noqa: E402

DEFAULT_PATH = ROOT / "agent" / "state" / "feedback.jsonl"
# Strongest symptom first. The primary signal is the first of these that the incident has.
PRIORITY = ["oom_killed", "image_pull_error", "config_error", "crash_loop", "liveness_probe_failed", "unschedulable",
            "dependency_down", "repeated_restarts", "not_ready"]
SAFE = re.compile(r"[^a-zA-Z0-9_.:\-]")


def signature(inc):
    """Stable identity of a failure across incidents: root app plus its strongest signal type."""
    types = {s.get("type") for s in inc.get("signals", [])}
    primary = next((t for t in PRIORITY if t in types), None) or (sorted(t for t in types if t)[0] if types else "none")
    return SAFE.sub("_", f"{inc['app']}:{primary}")[:120]


def family(fix_class):
    """`llm-revert-change` and `revert-recent-change` are one family; `llm-fix-env-var` and `fix-env-var` likewise."""
    c = str(fix_class).removeprefix("llm-")
    return {"revert-recent-change": "revert-change"}.get(c, c)


class FeedbackStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path) if path else None
        self.mem = read_events(self.path) if self.path else []

    def refresh(self):
        """Pick up decisions written by another process (the escalator, a human using the CLI)."""
        if self.path:
            self.mem = read_events(self.path)

    def add(self, event, **kw):
        ev = {"ts": time.time(), "event": event, **kw}
        self.mem.append(ev)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        return ev

    # --- queries ---------------------------------------------------------------------
    def decisions(self):
        """(signature, family) -> latest decision event (rejected | approved | forgotten)."""
        out = {}
        for e in self.mem:
            if e.get("event") in ("rejected", "approved", "forgotten") and e.get("signature") and e.get("fix_class"):
                out[(e["signature"], family(e["fix_class"]))] = e
        return out

    def rejection(self, sig, fix_class):
        """The standing rejection event for this signature and fix family, or None."""
        self.refresh()
        d = self.decisions().get((sig, family(fix_class)))
        return d if d and d["event"] == "rejected" else None

    def approvals(self):
        return [e for e in self.mem if e.get("event") == "approved"]

    # --- ingest ------------------------------------------------------------------------
    def ingest(self, incidents, states):
        """Fold PR outcomes from the loops' state files into the store. Idempotent. `incidents`: id -> incident dict.
        `states`: iterables of state events. Returns the new decision events."""
        self.refresh()
        seen = {(e.get("source"), e.get("pr")) for e in self.mem if e.get("event") in ("rejected", "approved")}
        new = []
        for events in states:
            events = list(events)
            opened = {e["pr"]: e for e in events if e.get("event") == "pr_opened"}
            for e in events:
                if e.get("event") not in ("pr_merged", "pr_closed") or e.get("pr") not in opened:
                    continue
                po = opened[e["pr"]]
                inc = incidents.get(po.get("incident"))
                key = (po.get("incident"), e["pr"])
                if inc is None or key in seen:
                    continue
                seen.add(key)
                kind = "approved" if e["event"] == "pr_merged" else "rejected"
                new.append(self.add(kind, signature=signature(inc), fix_class=po.get("playbook"), app=po.get("app"),
                                    incident=po.get("incident"), pr=e["pr"], source=po.get("incident"), url=po.get("url"),
                                    via="pull-request", reason="PR closed without merging" if kind == "rejected" else "PR merged"))
        return new


def _incidents(path):
    last = {}
    for ev in read_events(path):
        if "incident" in ev:
            last[ev["incident"]["id"]] = ev["incident"]
    return last


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "ingest", "reject", "forget"):
        p = sub.add_parser(name)
        p.add_argument("--feedback", default=str(DEFAULT_PATH))
        if name == "ingest":
            p.add_argument("--incidents", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
            p.add_argument("--state", action="append", help="fastpath / fixloop state file (repeatable)")
        if name in ("reject", "forget"):
            p.add_argument("signature"); p.add_argument("fix_class")
        if name == "reject":
            p.add_argument("--reason", default="rejected by a human (CLI)")
    a = ap.parse_args()
    store = FeedbackStore(a.feedback)
    if a.cmd == "list":
        rows = store.decisions()
        for (sig, fam), e in sorted(rows.items()):
            print(f"{e['event']:<9} {sig:<40} {fam:<26} {e.get('url') or ''} {e.get('reason') or ''}")
        print(f"{len(rows)} decision(s); {sum(1 for e in rows.values() if e['event'] == 'rejected')} standing rejection(s)")
    elif a.cmd == "ingest":
        states = a.state or [str(ROOT / "agent" / "state" / n) for n in ("fastpath.jsonl", "fixloop.jsonl")]
        new = store.ingest(_incidents(a.incidents), [read_events(s) for s in states])
        print(f"{len(new)} new decision(s)")
    elif a.cmd == "reject":
        store.add("rejected", signature=SAFE.sub("_", a.signature), fix_class=a.fix_class, via="cli", reason=a.reason[:300])
        print(f"recorded: {a.fix_class} will not be proposed again for {a.signature}")
    elif a.cmd == "forget":
        store.add("forgotten", signature=SAFE.sub("_", a.signature), fix_class=a.fix_class, via="cli", reason="cleared by a human")
        print(f"cleared: {a.fix_class} may be proposed again for {a.signature}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
