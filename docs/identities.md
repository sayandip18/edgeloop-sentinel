# Identities and permissions

Principle 4: separate roles, separate identities. Each identity can do exactly one kind of thing, and none of them can do another's job.

| Identity | Where it lives | Credential | Can do | Cannot do |
| --- | --- | --- | --- | --- |
| `diagnoser` | ServiceAccount in `observability` | Short-lived token via `scripts/make-kubeconfig.sh diagnoser` | get/list/watch pods, pods/log, events, configmaps, deployments, replicasets **in `apps` only** | any write, secrets, exec, anything outside `apps`, nodes |
| `fixer` | ServiceAccount in `observability` with **no bindings** | Off-cluster Git credential (see below) | Push a branch and open a PR in this repo | Anything in Kubernetes (denied everything) |
| `deployer` | ServiceAccount in `flux-system` | None issued; Flux impersonates it | create/update/patch/delete configmaps, services, deployments, replicasets **in `apps` only** | Anything else, including RBAC, secrets, other namespaces |
| `obs-deployer` | ServiceAccount in `flux-system` | None issued; Flux impersonates it | Install the observability stack: CRDs, ClusterRoles, workloads in `observability` (Phase 1) | Write to `apps`, read secrets outside `observability`, exec. Effectively privileged because charts create ClusterRoles: review changes like a cluster-admin grant |

Definitions: [infra/rbac/](../infra/rbac/). Verified by `scripts/verify-phase0.sh`.

## Permission matrix (what the verifier asserts)

| Action (namespace `apps` unless noted) | diagnoser | fixer | deployer |
| --- | --- | --- | --- |
| get/list/watch pods, pods/log, events, configmaps, deployments, replicasets | yes | no | yes (workload kinds only; no pods/log/events) |
| create / update / patch / delete workloads | **no** | no | yes (configmaps, services, deployments, replicasets) |
| read secrets | **no** | no | no |
| `pods/exec` | **no** | no | no |
| read pods in `kube-system` | no | no | no |
| API discovery (`/version`, `/api`, self-review) | yes | yes | yes |

The last row is Kubernetes' default `system:basic-user` / `system:discovery` access for any authenticated user. It exposes no cluster data, so the verifier allows it and rejects anything else for the fixer.

## Fixer's Git credential (used from Phase 4; not created yet)

Decision: a **fine-grained personal access token** scoped to this one repository, with:

- Contents: read and write
- Pull requests: read and write
- Nothing else (no Actions, no Administration, no other repos)

Preferably owned by a **separate bot account** that is a collaborator on the repo, so commits and PRs are clearly attributable to the agent and the token is not tied to the maintainer's account. Set a short expiry and rotate it.

Why not a write deploy key: a deploy key can push but cannot open pull requests, and principle 2 says every fix is a PR.

Handling rules for when it is created:

- Store it outside the repo (env var or secret store). `.gitignore` already covers `.env`, `*.pem`, `*.key`.
- Never mount it into the cluster or give it to the diagnoser process.
- Without branch protection on `main` the PAT *could* push to `main` directly. Protection is required from Phase 4 on, so "fixer proposes, human approves" is enforced by GitHub and not by convention. `agent/fastpath.py` refuses to start unless GitHub reports `main` as protected and the token's account is not a repo admin. Setup steps: [phase4.md](phase4.md#one-time-setup-owner-actions).

## Gotchas

### Flux impersonation: the deployer ServiceAccount must be in `flux-system`

Each Flux Kustomization sets `spec.serviceAccountName: deployer`. Flux resolves that name in the **Kustomization's own namespace** (`flux-system`), not in `targetNamespace` (`apps`). So the ServiceAccount lives in `flux-system`, while its Role and RoleBinding live in `apps`. If the ServiceAccount is put in `apps`, Flux cannot find it and the Kustomization fails to reconcile.

Consequence of the narrow Role: a new app that uses another kind (for example an Ingress or a PVC) fails to apply with `forbidden` until `infra/rbac/deployer.yaml` is extended. That is intended: widening the deployer is a reviewed Git change. The Role must also stay out of scope for RBAC objects, or the deployer could grant itself more.

### Flux has no write access to Git

Flux was installed with `flux install` plus the manifests in `infra/flux/`, never `flux bootstrap`. The repo is public, so Flux pulls over HTTPS with no credential. Flux cannot push, and there is no deploy key to leak.

### Kubernetes 1.34 vs Flux 2.10

Docker Desktop provides Kubernetes v1.34.1, but Flux 2.10 declares support for >= 1.35. `flux check` therefore reports one failing item (the Kubernetes version). Everything exercised so far works, including apply, impersonation as `deployer`, and drift correction. Treat that single failure as expected. It goes away with the move to a k3s VM before Phase 5.

### kubeconfigs are bearer tokens

`scripts/make-kubeconfig.sh` writes `kubeconfigs/<identity>.kubeconfig` (gitignored) with a token valid for 1h by default. It refuses to issue one for `deployer`.
