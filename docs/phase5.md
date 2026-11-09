# Phase 5: Safety rails

**Exit criterion:** deliberately bad patches are blocked by the gate or reverted by the watchdog.

**Status: implemented, verified offline** (`bash scripts/verify-phase5.sh`). Not yet run: the live vcluster sandbox (`--cluster`) and a live watchdog revert against GitHub. Nothing here has been committed.

## Decisions taken

| Decision | Choice | Why |
| --- | --- | --- |
| k3s VM move | **Deferred**, not needed for Phase 5 | Everything uses a kube context (`EDGELOOP_CONTEXT`), so Docker Desktop keeps working before and after. The VM is wanted for NetworkPolicy enforcement, gVisor and a truly separate host for the watchdog. See "k3s move" below. |
| Sandbox | **vcluster** | Runs inside the existing cluster; no Docker-in-Docker. |
| Policy engine | **Python gate** (`policy/gate.py`, rules in `policy/rules.yaml`) | No new in-cluster controllers; deterministic and testable offline. |
| Rollback | **Host watchdog** (`watchdog/watchdog.py`) | Outside Kubernetes, as the README requires. No Argo Rollouts or Flagger: with one replica and one site there is nothing to canary yet. |
| Tools | kubeconform, kube-linter, vcluster installed with winget | Approved. |

## What was built

    fastpath.py -> playbook plan -> kustomize check -> policy/gate.py (separate process) -> policy/risk.py -> PR (L1)
                                                                    blocked / L0 -> declined + escalate
    human merges -> Flux applies -> watchdog/watchdog.py watches health -> confirmed | revert commit pushed to main

### Validation gate, `policy/gate.py` (independent of the author)
It imports nothing from `agent/` or `playbooks/` and runs as its own process. It gets a base Git ref and the proposed file contents, rebuilds both worlds itself and judges the difference. All rules are data in `policy/rules.yaml`.

| Check | Blocks |
| --- | --- |
| scope | any path outside `apps/<app>/{base,overlays}/*.yaml`; more than 1 app, 4 files or 40 changed lines |
| blast radius | RBAC, ServiceAccount, NetworkPolicy, PV/PVC, StorageClass, Namespace, Secret, CRDs, webhooks, by file name, file content and built output; kinds outside Deployment/Service/ConfigMap; namespaces other than `apps`; remote or cross-app kustomize references |
| kustomize | the app must build the way Flux builds it |
| schema | kubeconform (strict) on the built output |
| lint | kube-linter on before and after: only **new** findings fail |
| policy | privileged, host namespaces, hostPath, added capabilities, root, privilege escalation, writable rootfs, images not matching `edgeloop/<name>:v<N>` (so no `:latest`, no foreign registry), missing or excessive limits (512Mi / 1000m), more than 3 replicas |
| sandbox (`--sandbox`) | patched manifests that do not become healthy and restart-free in a vcluster |

Verdict: `pass`, `blocked`, or `incomplete` (a required tool is missing). The fast path treats `incomplete` as blocked: a skipped safety check is never a pass.

### Risk engine, `policy/risk.py` + `policy/autonomy.yaml`
Maps a gated change to an autonomy level. Effective level = the lower of the fix class's ceiling and what the risk score allows (lines changed, touching shared `base/`, high-criticality app, memory more than doubling, deleted files). An unknown fix class gets L0 (notify only). Nothing above L1 is returned until Phase 9.

### Sandbox, `policy/sandbox.py`
Creates a vcluster in its own host namespace, applies the current manifests (the failure should reproduce), swaps in the patched ones, requires every pod Ready with zero restarts for a stable window, and always deletes the vcluster and namespace. If the failure does not reproduce the result says so (inconclusive), but only recovery is required to pass.

### Rollback watchdog, `watchdog/watchdog.py`
Watches `origin/main` for commits whose subject starts `sentinel(` and tracks the changed app with read-only `kubectl get` (diagnoser kubeconfig). It confirms once the app has been healthy for `--stable` seconds after the change was applied. It reverts (a `git revert` pushed to `main` from a throwaway clone, with **the host's own git credentials**) if the app is unhealthy for `--fail-after` seconds after applying, or is not confirmed by `--timeout`. Silence means rollback, and an unreachable API counts as unhealthy. It ignores human and eval-injected commits, never forces a conflicting revert (logs `revert_failed`), and honours `--dry-run` and a `watchdog/DISABLE` kill-switch file. The revert subject starts with "Revert", so the playbooks never revert a revert.

## Running it

    bash scripts/verify-phase5.sh              # offline, no cluster, no pushes
    bash scripts/verify-phase5.sh --cluster    # + live vcluster reproduce-then-fix

    python policy/gate.py check --base-ref origin/main --head my-branch      # gate any branch
    python agent/fastpath.py run --sandbox                                   # fast path with the live sandbox too
    python watchdog/watchdog.py run --dry-run                                # log what it would revert
    python watchdog/watchdog.py run                                          # live: pushes revert commits to main

The watchdog needs git credentials that can push to `main` (you are the admin; the bot is not) and `bash scripts/make-kubeconfig.sh diagnoser 8h`.

## Caveats and honest gaps

- **Offline-verified only.** The sandbox and the live watchdog revert have not run against the real cluster or GitHub (the cluster was down while building). The watchdog logic is tested against a local bare repo with fake health and a fake clock.
- **Egress is not enforced on Docker Desktop.** The sandbox declares a default-deny NetworkPolicy, but Docker Desktop's CNI ignores it. Enforcement and gVisor come with the k3s VM.
- **The sandbox runs under your admin context** (`EDGELOOP_SANDBOX_CONTEXT`), since installing a vcluster needs namespace and Helm rights. A narrower validator identity is a follow-up.
- **Not covered:** image scan and signing (Trivy, cosign; images are local-only, so this waits for a registry and Phase 9); build and unit tests (the gate refuses code changes outright); progressive rollout with Argo Rollouts or Flagger (needs more than one replica or site; the watchdog covers rollback meanwhile); re-running the gate in CI as a required check at merge time (it currently runs before the PR is opened).
- **Watchdog "applied" signal** is a new Deployment generation or `--min-observe` seconds (90), so the old healthy state cannot confirm a change before Flux applied it. Timings are untuned.
- **The gate is slow on Windows** (kustomize, kube-linter and kubeconform per evaluation; the gate test file takes a few minutes). Fine per incident.
- Risk weights in `policy/autonomy.yaml` are a first guess, to be tuned against the eval harness.

## k3s move (optional follow-up)
Needed for NetworkPolicy enforcement in the sandbox, gVisor, and a watchdog on a different machine from the cluster. Steps: provision a Linux VM (Multipass, Hyper-V or WSL2), install k3s, copy its kubeconfig, set `EDGELOOP_CONTEXT`, import the demo images into k3s (`imagePullPolicy: Never` needs them on the node), rerun `scripts/up.sh`, the Flux install, RBAC and the Phase 0 verify. No agent code changes are required.
