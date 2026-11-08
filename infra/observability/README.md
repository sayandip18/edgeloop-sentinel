# infra/observability

Phase 1 stack, deployed by Flux (not by hand). Lean profile sized for Docker Desktop (about 7.4 GiB).

| Dir | Contents |
| --- | --- |
| `rbac/` | `obs-deployer` identity (separate from `deployer`) |
| `sources/` | HelmRepository objects |
| `releases/` | HelmReleases: kube-prometheus-stack (lean), Loki (single binary), Alloy |

Flux Kustomization `observability` (see `infra/flux/observability.yaml`) applies `sources/` and `releases/` as `obs-deployer`. `rbac/` is applied by Flux too, but under the Flux admin identity, since an identity must not grant itself permissions (see `infra/flux/observability-rbac.yaml`).
