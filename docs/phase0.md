# Phase 0: lab, GitOps, scoped identities

**Exit criterion:** a Git commit changes the cluster, and the diagnoser identity is provably read-only.

## Environment

| Item | Value |
| --- | --- |
| Cluster | Docker Desktop Kubernetes (kubeadm, v1.34.1), context `docker-desktop`. Not kind. |
| Images | `python:3.12-slim`, built locally as `edgeloop/<app>:v1`, `imagePullPolicy: Never`. No registry. |
| GitOps | Flux 2.10 in `flux-system`, pulling this public repo over HTTPS (1m interval, branch `main`) |
| Host | Windows 11, Git Bash. `make` is not installed; run `bash scripts/<name>.sh` directly. |

Known warning: `flux check` reports one failing item because Flux 2.10 wants Kubernetes >= 1.35. See [identities.md](identities.md#kubernetes-134-vs-flux-210). Plan to move to a k3s VM before Phase 5.

## What exists

- **Four demo apps** in `apps/{oom-app,badconfig-app,crash-app,healthy-app}`, each with `base/` and `overlays/{healthy,broken}`, deployed to namespace `apps`.

  | App | Broken overlay produces |
  | --- | --- |
  | oom-app | OOMKilled (exit 137) |
  | badconfig-app | exit 2 |
  | crash-app | CrashLoopBackOff |
  | healthy-app | Nothing: its "broken" overlay is a decoy that must stay Ready and must never be "fixed" |

- **Flux config** in `infra/flux/`: one GitRepository and one Flux Kustomization per app, each pointing at `./apps/<app>/overlays/healthy`, `prune: true`, `targetNamespace: apps`, `serviceAccountName: deployer`.
- **Identities** in `infra/rbac/`: diagnoser, fixer, deployer. See [identities.md](identities.md).
- **Scripts** in `scripts/`:

  | Script | Purpose |
  | --- | --- |
  | `check-tools.sh` | Report missing tools (helm and kubeconform are currently missing) |
  | `up.sh` | Check the context and create namespaces |
  | `build-images.sh` | Build the demo images locally |
  | `make-kubeconfig.sh <diagnoser\|fixer> [duration]` | Issue a short-lived kubeconfig into `kubeconfigs/` |
  | `verify-phase0.sh [--git] [--yes]` | Verify the exit criterion |

## Reproduce

```bash
bash scripts/check-tools.sh
bash scripts/up.sh                                   # namespaces
bash scripts/build-images.sh                         # local images
flux install                                         # Flux controllers (not `flux bootstrap`)
kubectl apply -f infra/rbac/                         # identities
kubectl apply -f infra/flux/                         # GitRepository + Kustomizations
flux get kustomizations                              # all four Ready
kubectl -n apps get pods                             # all Running
```

## Verify

```bash
bash scripts/verify-phase0.sh          # cluster-only checks, no Git writes
bash scripts/verify-phase0.sh --git    # also commits and pushes (asks first; --yes skips the prompt)
```

| Check | Needs push? | How it is verified |
| --- | --- | --- |
| Flux kustomizations all Ready | no | `kubectl get kustomizations` |
| Diagnoser can read, cannot write, read secrets, or exec; denied outside `apps` | no | `kubectl auth can-i` plus real API calls with the issued kubeconfig |
| Fixer is denied everything | no | `can-i` checks and `can-i --list` (only API discovery and self-review allowed) |
| Manual `kubectl` edit is reverted by Flux | no | Scale `healthy-app` and change its image; wait for Flux to restore both |
| A Git change converges within 2 minutes | **yes** | Change `healthy-app` replicas 1 to 2, push, wait without forcing a reconcile |
| A broken overlay deployed via Git fails as designed | **yes** | Patch oom-app's healthy overlay to `ALLOC_MB=300`, push, expect OOMKilled; `healthy-app` must stay Ready |

The `--git` checks require a clean tree on `main` equal to `origin/main`. Each change is undone with `git revert` and pushed, so a run leaves 4 commits in history. A cleanup trap reverts a leftover test commit if the script dies midway.

### Results

Cluster-only checks (run 2026-11-08): **50 passed, 0 failed.**

- RBAC for diagnoser and fixer behaves as the matrix in [identities.md](identities.md) states.
- Drift correction works with Flux defaults (no configuration change needed): a manual scale and image change on `healthy-app` were reverted 43s after the edit, within the 1m interval.

Git-driven checks: **not yet run.** Record the result here after `--git` is run.

## Out of scope for Phase 0

- `agent/` is not built. The diagnosis agent is Phase 6 and the fix agent is Phase 7; Phase 0 only creates the identities they will use.
- The fixer's Git credential is decided (fine-grained PAT) but deliberately not created. See [identities.md](identities.md#fixers-git-credential-phase-7-not-created-yet).
- No branch protection on `main`. Add it before Phase 7.
