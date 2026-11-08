# Phase 1: Observability

**Exit criterion:** break each demo app via Git and confirm the failure is visible: OOMKilled and restarts in metrics, exit code and log lines in Loki, and `healthy-app` showing nothing wrong.

**Status: implemented, not yet verified on the cluster.** Manifests are written and pass `helm template` and server-side dry-run; they take effect only once pushed to `main` (Flux pulls from GitHub). Then run `bash scripts/verify-phase1.sh [--git]`.

## Decisions

| Decision | Choice |
| --- | --- |
| Stack size | Lean (Docker Desktop has about 7.4 GiB): Prometheus + operator + kube-state-metrics, Loki single binary, Alloy. No Grafana, Alertmanager, node-exporter, admission webhooks or control-plane scrapes. Memory limits total roughly 1.7 GiB. |
| Deploy identity | New `obs-deployer` ServiceAccount (flux-system). `deployer` stays scoped to `apps`. |
| Diagnoser access to Prometheus/Loki | **Deferred to Phase 6.** The diagnoser has no route to either API yet (the verifier asserts it cannot proxy services in `observability`). Phase 6 will add a read-only query proxy that allows only query endpoints. Humans and the verifier use the API server service proxy with the admin context. |
| Log collection | Alloy as a Deployment tailing `apps` pods through the Kubernetes API (no hostPath mounts). Only `apps` is collected. |
| Exit code | Lives in Kubernetes state, so it is checked in metrics (`kube_pod_container_status_last_terminated_exitcode`), not Loki. Loki holds the log lines. |
| Storage | None persistent. Prometheus keeps 24h, Loki 24h, both lost on pod restart. Lab only. |

## What exists

- `infra/observability/`: HelmRepository + HelmReleases (chart versions pinned), `rules/apps.yaml` (four small alert rules for the demo failures), `rbac/obs-deployer.yaml`.
- `infra/flux/observability.yaml`: three ordered Flux Kustomizations, `observability-rbac` (Flux admin identity) -> `observability` (as obs-deployer) -> `observability-rules` (as obs-deployer; needs the CRDs the chart installs).
- `scripts/verify-phase1.sh`: stack health, scrape targets, logs flowing, identity boundaries; with `--git`, breaks oom/badconfig/crash in one commit, checks metrics and Loki for each, checks the decoy, reverts.

## Caveats

- **`obs-deployer` is effectively privileged.** Charts create ClusterRoles, so it needs `escalate`/`bind`. It has no write access to `apps` and no secrets outside `observability`, but treat edits to `rbac/obs-deployer.yaml` like a cluster-admin grant.
- HelmRelease is `helm.toolkit.fluxcd.io/v2` on Flux 2.10 (v1 is not served).
- Permissions in `obs-deployer` were derived from `helm template` output, not from a live run. If a HelmRelease fails with `forbidden`, extend the Role in Git.

## Deploy and verify

    kubectl apply -f infra/flux/observability.yaml   # one-time, like the other infra/flux files
    # (or: commit infra/ to main and apply the file; Flux then reconciles everything else)
    bash scripts/verify-phase1.sh           # stack checks
    bash scripts/verify-phase1.sh --git     # + failure injection (commits and pushes)

Quick look by hand:

    kubectl -n observability port-forward svc/kps-prometheus 9090
