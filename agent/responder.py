#!/usr/bin/env python3
"""Eval responder shim (Phase 6): adapts the diagnosis agent to the eval harness's `--responder` protocol.

    python evals/run.py run all --detector --responder "python agent/responder.py"
    python evals/run.py run all --detector --responder "python agent/responder.py --baseline"     # rules-only baseline

The harness runs this with env EVAL_SCENARIO_ID, EVAL_INJECT_APP, EVAL_OBSERVE_APP, EVAL_INJECT_SHA and reads the LAST
stdout line as JSON: {"action": ..., "diagnosis": {"category", "target_app", "root_cause"}}. All logging goes to stderr.

It contains no diagnosis logic. It finds the incident (from the detector's log, or by running one detection pass
itself), builds the evidence bundle, runs the agent, writes a `diagnosed` event to the incident log, and prints the result.
Actions: "diagnosed" (a fault was identified, nothing is done: Phase 6 never fixes), "none" (no fault), "escalate"
(low confidence, invalid output or an error). It never prints "fix".

--baseline: no LLM. The rules-only comparison point: the detector's strongest signal gives the category, and a playbook
that applies (playbooks.select) gives the target app (and `oom` for raise-memory-limit). It cannot tell apart causes
that share a signal. The LLM run also prints this baseline's diagnosis for the same incident, so one run scores both.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import playbooks  # noqa: E402
from budget import Budget  # noqa: E402
from collector import Collector, CollectorError  # noqa: E402
from detector import detect  # noqa: E402
from service import run_diagnosis  # noqa: E402
from gitrepo import GitRepo  # noqa: E402
from incidents import IncidentManager, read_events  # noqa: E402

FRESH_S = 1800


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def find_incident(log_path, apps, now=None):
    """Most recent incident (any state) in the detector's log that is rooted at, or affects, one of `apps`."""
    now = time.time() if now is None else now
    last = {}
    for ev in read_events(log_path):
        if "incident" in ev:
            last[ev["incident"]["id"]] = ev["incident"]
    cands = [i for i in last.values() if (i["app"] in apps or set(i.get("affected_apps", [])) & set(apps)) and now - i["last_seen"] <= FRESH_S]
    return max(cands, key=lambda i: i["opened_at"], default=None)


def incident_from_cluster(col, apps):
    """No detector log: run one detection pass now. May return None (nothing flagged, or still inside a grace period)."""
    now = datetime.now(timezone.utc)
    mgr = IncidentManager(None)
    mgr.observe(detect(col.snapshot(), now=now), now=now.timestamp())
    for inc in mgr.open.values():
        if inc["app"] in apps or set(inc["affected_apps"]) & set(apps):
            return inc
    return None


# signal type -> category: the only inference a rules-only system can make
BASELINE = [("oom_killed", "oom"), ("image_pull_error", "bad_image"), ("config_error", "bad_config"),
            ("liveness_probe_failed", "probe_failure"), ("crash_loop", "app_crash"), ("repeated_restarts", "app_crash")]


def baseline_diagnosis(incident, repo=None):
    """Rules-only baseline = what the Phase 3 detector and Phase 4 playbooks implicitly know, no LLM:
    category from the strongest signal (BASELINE map); a playbook that applies (playbooks.select) supplies the target
    app and, for raise-memory-limit, the category (oom). `playbook` records which one applied. It cannot tell apart
    causes that share a signal, which is the point of the comparison."""
    if not incident or not incident["signals"]:
        return {"action": "none", "diagnosis": {"category": "none", "target_app": "", "root_cause": "no signals"}, "playbook": None}
    types = {s["type"] for s in incident["signals"]}
    cat = next((c for t, c in BASELINE if t in types), None)
    if cat is None:
        cat = "dependency_outage" if any(s["type"] == "not_ready" and s["app"] != incident["app"] for s in incident["signals"]) else "probe_failure"
    target, playbook = incident["app"], None
    if repo is not None:
        try:
            plan, _ = playbooks.select(incident, repo)
            if plan:
                playbook, target = plan.playbook, plan.app
                if plan.playbook == "raise-memory-limit":
                    cat = "oom"
        except Exception as e:  # git unavailable: fall back to signals only
            log(f"baseline: playbook lookup failed ({e})")
    root = "; ".join(s["detail"] for s in incident["signals"])[:300]
    return {"action": "diagnosed", "diagnosis": {"category": cat, "target_app": target, "root_cause": root}, "playbook": playbook}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubeconfig", default=str(ROOT / "kubeconfigs" / "diagnoser.kubeconfig"))
    ap.add_argument("--incidents", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--app", help="app to review (default: EVAL_OBSERVE_APP / EVAL_INJECT_APP)")
    ap.add_argument("--incident", help="incident id to diagnose (default: the latest for the app)")
    ap.add_argument("--min-confidence", type=float, default=0.6)
    a = ap.parse_args()

    apps = [x for x in (a.app, os.environ.get("EVAL_OBSERVE_APP"), os.environ.get("EVAL_INJECT_APP")) if x]
    if not apps:
        log("no app given (EVAL_OBSERVE_APP or --app)"); print(json.dumps({"action": "escalate", "diagnosis": None, "error": "no app"})); return 2
    col = Collector(a.kubeconfig)
    try:
        col.assert_read_only()
        left = col.token_seconds_left()
        if left is not None and left < 600:
            log(f"WARNING: the diagnoser token {'expired' if left <= 0 else f'expires in {int(left)}s'}; refresh: bash scripts/make-kubeconfig.sh diagnoser 8h")
        incident = None
        if a.incident:
            incident = next((e["incident"] for e in read_events(a.incidents) if e.get("incident", {}).get("id") == a.incident), None)
        incident = incident or find_incident(a.incidents, apps) or incident_from_cluster(col, apps)
        log(f"incident: {incident['id'] if incident else 'none found'} (apps {apps})")

        repo = GitRepo(ROOT)
        try:
            repo.fetch("origin")
        except Exception as e:
            log(f"git fetch failed ({e}); continuing without a fresh Git view")
            repo = None

        if a.baseline:
            out = baseline_diagnosis(incident, repo)
            out["via"] = "baseline"
            print(json.dumps(out)); return 0

        try:
            budget = Budget.from_env()
        except ValueError as e:
            log(f"budget misconfigured: {e}")
            print(json.dumps({"action": "escalate", "diagnosis": None, "error": f"budget misconfigured: {e}"})); return 2
        res = run_diagnosis(col, repo, incident, apps[0], min_confidence=a.min_confidence, budget=budget, incidents_log=a.incidents)
        log(f"{res['action']}: {res['reason']} (model {res['model']}, {res['tokens']} tokens, {len(res['tool_calls'])} tool calls, {res.get('seconds')}s; "
            f"run {budget.run_id!r} spent {budget.spent()})")
        if res.get("error"):
            log(res["error"])
        top = res["diagnosis"]["hypotheses"][0] if res["diagnosis"] else None
        print(json.dumps({"action": res["action"], "via": "llm", "baseline": baseline_diagnosis(incident, repo)["diagnosis"],
                          "model": res["model"], "tokens": res["tokens"], "reason": res["reason"],
                          "injection_suspected": res["diagnosis"]["injection_suspected"] if res["diagnosis"] else None,
                          "confidence": top["confidence"] if top else None,
                          "diagnosis": ({k: top[k] for k in ("category", "target_app", "root_cause")} if top else None)}))
        return 0
    except CollectorError as e:
        log(f"cluster read failed: {e}")
        print(json.dumps({"action": "escalate", "diagnosis": None, "error": str(e)[:200]})); return 1


if __name__ == "__main__":
    sys.exit(main())
