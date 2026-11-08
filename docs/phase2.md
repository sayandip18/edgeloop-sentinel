# Phase 2: Failure catalog and eval harness

**Exit criterion:** one command injects and scores a failure: `bash scripts/eval.sh run oom-kill`.

**Status: implemented, not yet run end to end.** Offline checks pass (`bash scripts/verify-phase2.sh`). The end-to-end run needs the Phase 2 files pushed to `main` (Flux reads GitHub) and `infra/flux/kustomizations.yaml` re-applied. Nothing here has been committed.

## How a run works

1. **Precheck:** on `main`, clean tree, equal to `origin/main`; every app the scenario touches is healthy (otherwise it refuses to inject).
2. **Inject through Git:** copy `apps/<app>/overlays/<overlay>/kustomization.yaml` over that app's `overlays/healthy/`, commit, push to `main`. Flux applies it within about a minute. (Flux only watches `origin/main` and GitOps is the only write path, so a change that is not on `main` does nothing.)
3. **Wait for the symptom** via kubectl (no dependency on Phase 1): a container reason, N restarts, pod never Ready, or, for the decoy, that nothing goes wrong for 90s.
4. **Responder (optional):** a command that stands in for the future agent (see the protocol in `evals/run.py`). Phase 2 has none, so fix and diagnosis metrics are `null`, not 0.
5. **Revert through Git** (`git revert` + push) and wait for recovery. This proves the harness restores a clean baseline, so the next scenario starts healthy.
6. **Score** and write `evals/results/<timestamp>.json`.

## Scenarios (`evals/scenarios/`)

| Scenario | App injected | Symptom | Expected category | Expected action |
| --- | --- | --- | --- | --- |
| oom-kill | oom-app | OOMKilled | oom | fix |
| bad-config | badconfig-app | restarts (exit 2) | bad_config | fix |
| app-crash | crash-app | CrashLoopBackOff | app_crash | fix |
| bad-image-tag | web-app | ErrImageNeverPull | bad_image | fix |
| wrong-env-var | web-app | restarts (startup exit 1) | bad_env_var | fix |
| failing-probe | web-app | restarts (liveness 404) | probe_failure | fix |
| dependency-outage | backend-app (observed on web-app) | Running but not Ready | dependency_outage, target `backend-app` | fix |
| decoy-noisy-healthy | healthy-app | none: scary ERROR logs, stays Ready | none | **none** |

New apps: `backend-app` (HTTP backend + Service) and `web-app` (depends on it; readiness checks the backend, liveness is `/healthz`). Each scenario's expected diagnosis has a category, the true target app, and keywords the root cause must mention. The dependency outage deliberately has a different target from the app showing symptoms.

## Metrics

| Metric | Definition |
| --- | --- |
| fix rate | fixed / scenarios with action `fix` where a responder ran |
| false-fix rate | decoys where the responder proposed a fix, or anyone else pushed to `main` during the window |
| time to recovery | from the inject push until all recovery apps are healthy, when the responder fixed it |
| diagnosis accuracy | category AND target app AND all keywords match |
| detect time | inject push until the symptom is visible (a harness number, not an agent number) |
| harness_ok | symptom observed (decoy: stayed healthy) and the revert recovered. The only pass/fail available without a responder |

## Caveats

- Every run pushes 2 commits per scenario to a public repo and leaves `main` describing a broken cluster for a few minutes. Testing only; real fixes will be PRs (Phase 4+).
- A run that dies midway can leave the injection on `main`. The runner reverts in its normal path; after a crash check `git log` and revert by hand.
- Injection replaces `overlays/healthy` content, so scenarios share that file per app. Run scenarios sequentially (the runner does).
- Adds `pyyaml` to `requirements.txt`.

## Before the first real run

    git add -A && git commit && git push          # you decide; the runner needs the new apps on main
    kubectl apply -f infra/flux/kustomizations.yaml
    bash scripts/build-images.sh                   # already built locally
    bash scripts/verify-phase2.sh --git
