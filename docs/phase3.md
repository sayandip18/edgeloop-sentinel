# Phase 3: Detection and incident manager

**Exit criterion:** every Phase 2 scenario produces exactly one incident, and the decoy produces none.

**Status: implemented, not yet run end to end.** Offline and read-only live checks pass (`bash scripts/verify-phase3.sh`). The end-to-end run (`--git`) needs the Phase 2 apps on `main` and runs the full catalog through Git, like Phase 2. Nothing here has been committed.

## Pieces

- **Detector** (`agent/detector.py`): pure functions over raw pod, deployment and event JSON.
- **Incident manager** (`agent/incidents.py`): turns signals into incident records.
- **Collector** (`agent/collector.py`): `kubectl get pods,deployments,events` using `kubeconfigs/diagnoser.kubeconfig` (never a kube context). Before reading it checks the identity can read the workload kinds and **cannot** create/delete pods, patch deployments, read secrets or exec; otherwise it refuses to run (exit 2).
- **Loop** (`agent/watch.py`): polls every 5s, prints incident events, appends them to `agent/state/incidents.jsonl` (gitignored, replayed on restart).

## Signals

| Signal | Rule |
| --- | --- |
| `oom_killed` | container last terminated with reason OOMKilled within 10 min |
| `crash_loop` | container waiting in CrashLoopBackOff |
| `repeated_restarts` | restartCount >= 2 and last exit code non-zero, within 10 min |
| `image_pull_error` | waiting reason ErrImageNeverPull / ImagePullBackOff / ErrImagePull / InvalidImageName |
| `config_error` | CreateContainerConfigError / CreateContainerError |
| `liveness_probe_failed` | kubelet `Unhealthy` event "Liveness probe failed" for a current pod |
| `not_ready` | pod Running but not Ready for >= 45s |
| `unschedulable` | pod Pending with PodScheduled=False for >= 45s |
| `dependency_down` | an app with a symptom points (env URL host) at a deployment that is scaled to 0 or has no ready replicas |

Not used: **log text** (untrusted, and the decoy's whole point is scary logs on a healthy app). A single old restart is ignored (every app on this cluster has `restartCount=1` from a Docker Desktop restart), as are terminating pods and fresh pods inside the 45s rollout grace. A deployment scaled to 0 that nothing depends on is treated as intentional.

## Correlation rules

1. One open incident per **root app**. A signal goes to its `caused_by` app if the detector attributed it to a dependency, otherwise to the app showing it.
2. So the dependency outage is one incident on `backend-app` (the true target), with `web-app` under `affected_apps`. `wrong-env-var` points at `backend-svc`, which no deployment has, so it stays an incident on `web-app`.
3. Escalating signals on the same app (OOMKilled, then CrashLoopBackOff, then restarts) update the same incident.
4. An incident resolves after 45s with no signals; a later failure opens a new one.

An incident record has `id`, `app`, `affected_apps`, `state`, `opened_at`, `last_seen`, `resolved_at` and a list of signals with detail text. It deliberately has **no diagnosis**: that is Phase 6.

## Eval integration

`python evals/run.py run all --detector` starts `watch.py` (as the diagnoser) before each injection, waits for its baseline pass, then after the revert waits for incidents to resolve and scores them: exactly one incident on the expected target app for a `fix` scenario, none for the decoy. Results add `detection`, `incidents`, and aggregate `detection_rate` and `median_incident_s` (time from the inject push to the incident opening, an agent number unlike Phase 2's `detect_s`). The runner exits non-zero if any detection is wrong.

## Verify

    bash scripts/verify-phase3.sh            # no Git writes
    bash scripts/verify-phase3.sh --git      # full run: 16 commits pushed to main, all reverted

## Caveats and open points

- Needs a diagnoser token (1h). `make-kubeconfig.sh diagnoser 8h` for longer sessions; an expired token makes the watcher exit 2 with a clear message.
- A pod that restarted twice stays `repeated_restarts` for 10 minutes even if recovered, so its incident resolves late. Not an accuracy issue, only slow closing.
- Thresholds (45s grace, 10 min window, 2 restarts) are untuned guesses, checked only against synthetic snapshots until the first real run.
- Detection is polling at 5s, not a watch stream. Fine at this scale.
- Prometheus and Loki are unused. The diagnoser-to-Prometheus access decision from Phase 1 remains open and is not needed until Phase 6.
