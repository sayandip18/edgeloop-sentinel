#!/usr/bin/env python3
"""Diagnosis loop (Phase 6): diagnoses each new incident once and records the result in the incident log.

    python agent/diagnose_loop.py [--once] [--interval 10] [--settle 30] [--incidents PATH] [--dry-run]

A separate process from the detector (watch.py), so detection never depends on the LLM or the network. Read-only:
diagnoser kubeconfig, read-only Git view; its only write is appending a `diagnosed` event to the incident log.
It never fixes anything and never opens a PR. Phase 7 consumes the `diagnosed` events.

Rules
- One diagnosis per open incident, after `--settle` seconds so late signals are in.
- A failed attempt (API error, budget) is recorded as `escalate`; transient API errors are retried up to --max-attempts
  with --retry-after seconds between attempts. Escalation without a retry for budget exhaustion or invalid output.
- Honours the per-run budget (budget.py): once exhausted, incidents are recorded as `escalate` without calling the model.
- Exit codes: 0 ok, 2 refused to start (write-capable identity, bad credentials, misconfigured budget).
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from budget import Budget  # noqa: E402
from collector import Collector, CollectorError  # noqa: E402
from diagnose import record_diagnosis  # noqa: E402
from gitrepo import GitRepo  # noqa: E402
from incidents import read_events  # noqa: E402
from service import run_diagnosis  # noqa: E402
sys.path.insert(0, str(ROOT / "policy"))
import controls  # noqa: E402  (Phase 9: global kill switch)


def latest_incidents(path):
    last = {}
    for ev in read_events(path):
        if "incident" in ev:
            last[ev["incident"]["id"]] = ev["incident"]
    return last


def diagnosis_state(path):
    """incident id -> {'done': bool, 'attempts': int, 'last_ts': float} from the `diagnosed` events."""
    st = {}
    for ev in read_events(path):
        if ev.get("event") == "diagnosed":
            s = st.setdefault(ev["incident_id"], {"done": False, "attempts": 0, "last_ts": 0.0})
            s["attempts"] += 1; s["last_ts"] = ev["ts"]
            err = any(w in (ev.get("reason") or "") for w in ("LLM call failed",))
            s["done"] = s["done"] or not err      # an API failure is retryable; anything else is final
    return st


def pending(path, *, now, settle_s, max_attempts, retry_after_s):
    """Open incidents that need a (further) diagnosis attempt now."""
    st, out = diagnosis_state(path), []
    for inc in latest_incidents(path).values():
        if inc["state"] != "open" or now - inc["opened_at"] < settle_s:
            continue
        s = st.get(inc["id"])
        if s is None or (not s["done"] and s["attempts"] < max_attempts and now - s["last_ts"] >= retry_after_s):
            out.append(inc)
    return sorted(out, key=lambda i: i["opened_at"])


def process(col, repo, path, *, budget, now, settle_s=30, max_attempts=2, retry_after_s=120, min_confidence=0.6,
            dry_run=False, out=print, client=None):
    """One pass. Returns the list of incident ids diagnosed."""
    done = []
    for inc in pending(path, now=now, settle_s=settle_s, max_attempts=max_attempts, retry_after_s=retry_after_s):
        if dry_run:
            out(f"dry-run   would diagnose {inc['id']} ({inc['app']})"); continue
        res = run_diagnosis(col, repo, inc, inc["app"], min_confidence=min_confidence, budget=budget, incidents_log=path, client=client, now=now)
        top = res["diagnosis"]["hypotheses"][0] if res["diagnosis"] else None
        out(f"diagnosed {inc['id']}: {res['action']} ({res['reason']})"
            + (f" top={top['category']}@{top['target_app']} conf={top['confidence']:.2f}" if top else "")
            + f" tokens={res['tokens']} tools={len(res['tool_calls'])}")
        if res.get("error"):
            out(f"          error: {res['error']}")
        done.append(inc["id"])
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubeconfig", default=str(ROOT / "kubeconfigs" / "diagnoser.kubeconfig"))
    ap.add_argument("--incidents", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--settle", type=float, default=30.0)
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--retry-after", type=float, default=120.0)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not Path(a.kubeconfig).is_file():
        print(f"kubeconfig not found: {a.kubeconfig}\nrun: bash scripts/make-kubeconfig.sh diagnoser", file=sys.stderr); return 2
    col = Collector(a.kubeconfig)
    try:
        col.assert_read_only()
        budget = Budget.from_env()
    except (CollectorError, ValueError) as e:
        print(str(e), file=sys.stderr); return 2
    repo = GitRepo(ROOT)
    print(f"diagnose_loop started incidents={a.incidents} run={budget.run_id!r} dry_run={a.dry_run}", flush=True)
    warned = False
    while True:
        if controls.killed():            # Phase 9 kill switch: no model calls while it is on
            if a.once:
                return 0
            time.sleep(a.interval)
            continue
        try:
            left = col.token_seconds_left()
            if left is not None and left < 600 and not warned:
                print(f"WARNING: the diagnoser token {'has expired' if left <= 0 else f'expires in {int(left)}s'}; run: bash scripts/make-kubeconfig.sh diagnoser 8h", flush=True)
                warned = True
            try:
                repo.fetch("origin")
            except Exception as e:
                print(f"git fetch failed ({e}); diagnosing without a fresh Git view", flush=True)
            process(col, repo, a.incidents, budget=budget, now=time.time(), settle_s=a.settle, max_attempts=a.max_attempts,
                    retry_after_s=a.retry_after, min_confidence=a.min_confidence, dry_run=a.dry_run, out=lambda m: print(m, flush=True))
        except CollectorError as e:
            if e.fatal:
                print(f"fatal: {e}", file=sys.stderr); return 2
            print(f"cluster read failed: {e}", flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
