"""Offline tests: synthetic Kubernetes snapshots shaped like what each Phase 2 scenario produces.
Run: python agent/test_detection.py   (also collectable by pytest). No cluster needed."""
import sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from detector import Config, detect  # noqa: E402
from incidents import IncidentManager, summarize  # noqa: E402

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(app, name=None, *, ready=True, ready_since=T0 - timedelta(hours=1), restarts=0, waiting=None, last_term=None,
        phase="Running", deleting=False):
    name = name or f"{app}-abc"
    cs = {"name": app, "restartCount": restarts, "state": {"running": {}} if not waiting else {"waiting": {"reason": waiting}}}
    if last_term:
        cs["lastState"] = {"terminated": last_term}
    meta = {"name": name, "labels": {"app": app}}
    if deleting:
        meta["deletionTimestamp"] = iso(T0)
    return {"kind": "Pod", "metadata": meta,
            "status": {"phase": phase, "startTime": iso(ready_since), "containerStatuses": [cs],
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False", "lastTransitionTime": iso(ready_since)}]}}


def term(reason="Error", code=1, at=T0):
    return {"reason": reason, "exitCode": code, "finishedAt": iso(at)}


def deploy(app, replicas=1, ready=1, env=None):
    return {"kind": "Deployment", "metadata": {"name": app},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": app, "env": env or []}]}}},
            "status": {"readyReplicas": ready}}


def event(podname, msg, reason="Unhealthy", at=T0):
    return {"kind": "Event", "reason": reason, "message": msg, "lastTimestamp": iso(at), "count": 3,
            "involvedObject": {"kind": "Pod", "name": podname}}


def baseline():
    """What the live cluster looks like: every app Ready, some with 1 old restart (Docker Desktop restarts)."""
    old = term("Error", 255, T0 - timedelta(hours=3))
    apps = ["oom-app", "badconfig-app", "crash-app", "healthy-app", "backend-app", "web-app"]
    return {"pods": [pod(a, restarts=1, last_term=old) for a in apps],
            "deployments": [deploy(a, env=[{"name": "BACKEND_URL", "value": "http://backend-app"}] if a == "web-app" else None) for a in apps],
            "events": []}


def with_app(snap, app, new_pod=None, new_deploy=None, extra_events=()):
    """Replace one app's pod(s)/deployment in a baseline snapshot."""
    s = {"pods": [p for p in snap["pods"] if p["metadata"]["labels"]["app"] != app] + ([new_pod] if new_pod else []),
         "deployments": [d for d in snap["deployments"] if d["metadata"]["name"] != app] + ([new_deploy] if new_deploy else []),
         "events": list(snap["events"]) + list(extra_events)}
    return s


def run_timeline(snaps, step=5, resolve_after=45.0, log=None):
    """snaps: list of (seconds_since_T0, snapshot). Returns the manager and every event it emitted."""
    m, evs = IncidentManager(log, resolve_after), []
    for t, snap in snaps:
        now = T0 + timedelta(seconds=t)
        evs += m.observe(detect(snap, now=now, cfg=Config()), now=now.timestamp())
    return m, evs


def opened(evs): return [e["incident"] for e in evs if e["event"] == "opened"]


# --- tests ----------------------------------------------------------------------
def test_healthy_baseline_is_silent():
    # includes restartCount=1 from 3h ago on every app: must NOT be an incident
    assert detect(baseline(), now=T0 + timedelta(seconds=10)) == []


def test_oom_kill_one_incident_through_escalation():
    # OOMKilled -> CrashLoopBackOff -> more restarts, over several minutes: still one incident
    t = lambda s: T0 + timedelta(seconds=s)
    snaps = []
    for sec, (waiting, r) in {10: (None, 1), 30: ("CrashLoopBackOff", 2), 90: ("CrashLoopBackOff", 4), 200: (None, 5)}.items():
        p = pod("oom-app", ready=False, restarts=r, waiting=waiting, last_term=term("OOMKilled", 137, t(sec - 5)), ready_since=t(0))
        snaps.append((sec, with_app(baseline(), "oom-app", p)))
    m, evs = run_timeline(snaps)
    inc = opened(evs)
    assert len(inc) == 1 and inc[0]["app"] == "oom-app", inc
    types = {s["type"] for s in m.open["oom-app"]["signals"]}
    assert {"oom_killed", "crash_loop"} <= types, types


def test_bad_config_and_crash_app_are_one_incident_each():
    for app, wait in (("badconfig-app", "CrashLoopBackOff"), ("crash-app", "CrashLoopBackOff")):
        p = pod(app, ready=False, restarts=3, waiting=wait, last_term=term("Error", 2, T0), ready_since=T0)
        m, evs = run_timeline([(10, with_app(baseline(), app, p)), (15, with_app(baseline(), app, p))])
        assert [i["app"] for i in opened(evs)] == [app], (app, opened(evs))


def test_bad_image_tag_while_old_pod_still_serves():
    new = pod("web-app", "web-app-new", ready=False, waiting="ErrImageNeverPull", phase="Pending", ready_since=T0)
    s = baseline(); s["pods"].append(new)
    m, evs = run_timeline([(10, s), (60, s)])
    inc = opened(evs)
    assert len(inc) == 1 and inc[0]["app"] == "web-app" and inc[0]["signals"][0]["type"] == "image_pull_error", inc


def test_wrong_env_var_is_web_app_not_backend():
    # backend-svc does not exist, backend-app is healthy: no dependency attribution
    s = with_app(baseline(), "web-app",
                 pod("web-app", ready=False, restarts=3, waiting="CrashLoopBackOff", last_term=term("Error", 1, T0), ready_since=T0),
                 deploy("web-app", env=[{"name": "BACKEND_URL", "value": "http://backend-svc"}]))
    m, evs = run_timeline([(10, s)])
    inc = opened(evs)
    assert len(inc) == 1 and inc[0]["app"] == "web-app" and inc[0]["affected_apps"] == [], inc


def test_failing_probe_restarts_and_events_merge():
    p = pod("web-app", restarts=3, last_term=term("Error", 137, T0 - timedelta(seconds=5)), ready_since=T0)
    s = with_app(baseline(), "web-app", p, extra_events=[event("web-app-abc", "Liveness probe failed: HTTP probe failed with statuscode: 404", at=T0)])
    m, evs = run_timeline([(10, s), (20, s)])
    inc = opened(evs)
    assert len(inc) == 1 and inc[0]["app"] == "web-app", inc
    assert {x["type"] for x in m.open["web-app"]["signals"]} == {"repeated_restarts", "liveness_probe_failed"}


def test_dependency_outage_attributed_to_backend():
    # backend-app scaled to 0, web-app Running but not Ready for >45s
    snap = baseline()
    snap = with_app(snap, "backend-app", None, deploy("backend-app", replicas=0, ready=0))
    snap = with_app(snap, "web-app", pod("web-app", ready=False, ready_since=T0),
                    deploy("web-app", env=[{"name": "BACKEND_URL", "value": "http://backend-app"}]))
    m, evs = run_timeline([(10, snap), (60, snap), (120, snap)])
    inc = opened(evs)
    assert len(inc) == 1, inc                      # exactly one incident, not one for each app
    assert inc[0]["app"] == "backend-app", inc     # the root cause, not the app showing symptoms
    final = m.open["backend-app"]
    assert final["affected_apps"] == ["web-app"]
    assert {s["type"] for s in final["signals"]} == {"not_ready", "dependency_down"}


def test_decoy_is_silent_even_with_scary_logs():
    # the decoy's only difference is log text, which the detector never reads: snapshot == baseline
    m, evs = run_timeline([(t, baseline()) for t in range(0, 120, 5)])
    assert evs == []


def test_rollout_grace_not_flagged():
    # a fresh pod that is not Ready yet (10s) during a normal rollout
    p = pod("web-app", ready=False, ready_since=T0 - timedelta(seconds=10))
    assert detect(with_app(baseline(), "web-app", p), now=T0) == []


def test_terminating_pod_ignored():
    p = pod("oom-app", ready=False, restarts=5, waiting="CrashLoopBackOff", last_term=term("OOMKilled", 137, T0), deleting=True)
    assert detect(with_app(baseline(), "oom-app", p), now=T0) == []


def test_scaled_to_zero_without_dependents_is_not_an_incident():
    snap = with_app(baseline(), "crash-app", None, deploy("crash-app", replicas=0, ready=0))
    assert detect(snap, now=T0) == []


def test_resolves_then_reopens_as_new_incident():
    bad = with_app(baseline(), "oom-app", pod("oom-app", ready=False, restarts=2, waiting="CrashLoopBackOff", last_term=term("OOMKilled", 137, T0), ready_since=T0))
    m, evs = run_timeline([(10, bad), (15, baseline()), (30, baseline()), (70, baseline()), (200, bad)])
    kinds = [(e["event"], e["incident"]["id"]) for e in evs if e["event"] in ("opened", "resolved")]
    assert [k[0] for k in kinds] == ["opened", "resolved", "opened"], kinds
    assert kinds[0][1] != kinds[2][1]


def test_flapping_signal_does_not_split_incident():
    bad = with_app(baseline(), "crash-app", pod("crash-app", ready=False, restarts=3, waiting="CrashLoopBackOff", last_term=term("Error", 1, T0), ready_since=T0))
    gap = with_app(baseline(), "crash-app", pod("crash-app", ready=False, restarts=3, last_term=term("Error", 1, T0), ready_since=T0))  # backoff gap: no 'waiting', still repeated_restarts
    m, evs = run_timeline([(10, bad), (20, gap), (30, bad), (40, gap)])
    assert len(opened(evs)) == 1


def test_incident_log_replay_survives_restart():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "inc.jsonl"
        bad = with_app(baseline(), "oom-app", pod("oom-app", ready=False, restarts=2, waiting="CrashLoopBackOff", last_term=term("OOMKilled", 137, T0), ready_since=T0))
        m1, _ = run_timeline([(10, bad)], log=log)
        m2 = IncidentManager(log)                       # new process, same log
        now = T0 + timedelta(seconds=20)
        evs = m2.observe(detect(bad, now=now), now=now.timestamp())
        assert not [e for e in evs if e["event"] == "opened"], "restart must not duplicate the open incident"
        incs, n_open = summarize(log)
        assert len(incs) == 1 and n_open == 1


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f(); print(f"ok    {n}")
        except Exception as e:
            failed += 1; print(f"FAIL  {n}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
