# edgeloop-sentinel

An autonomous self-healing agent for containerized edge services on Kubernetes. It detects runtime failures, diagnoses them from logs and cluster state, proposes patches to code or manifests, validates them in an isolated sandbox with independent machine checks, and then either rolls them out safely or escalates to a human.

> **Status:** early development. Phase 0 (lab, GitOps, scoped identities) is built; see [docs/phase0.md](docs/phase0.md) and the [Roadmap](#roadmap).

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Design principles](#design-principles)
- [Architecture](#architecture)
- [Zones and trust boundaries](#zones-and-trust-boundaries)
- [How a fix flows](#how-a-fix-flows)
- [Autonomy levels](#autonomy-levels)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Failure catalog and evaluation](#failure-catalog-and-evaluation)
- [Safety model](#safety-model)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Why this exists

Edge sites often have intermittent connectivity and no on-call engineer nearby. Most incidents are repetitive: a bad config rollout, an OOM kill, a failing probe, a stuck pod. edgeloop-sentinel handles known failures with deterministic playbooks, uses an LLM only for novel failures, and never lets any change reach the fleet without passing independent checks and a progressive rollout.

## Design principles

1. **Safety rails before intelligence.** Validation, sandboxing, and rollback are built before any LLM output is allowed through them.
2. **GitOps is the only write path.** Agents never mutate the cluster directly. Every fix is a Git PR, which gives auditability and trivial revert.
3. **Cheap and deterministic first.** Known failures use playbooks with no LLM. The LLM handles only what playbooks cannot.
4. **Separate roles, separate identities.** The diagnoser is read-only, the fixer can only write to the repo, and the deployer is GitOps-only.
5. **Independent verification.** The component that validates a patch is not the one that wrote it.
6. **Logs are untrusted input.** Log content is treated as data, never as instructions.
7. **Graduated autonomy.** Each fix class earns higher autonomy through measured results on the eval harness.

## Architecture

```
 WORKLOAD CLUSTER (edge)                    HEALER (control plane)
┌─────────────────────────┐   store-and-   ┌──────────────────────────────────────────┐
│ Workloads               │   forward      │ Incident Manager (dedupe, correlate)     │
│ Telemetry (OTel,        │──────────────► │        │                                 │
│  Fluent Bit, Falco)     │                │        ▼                                 │
│ Local detector          │                │ Context Builder (logs, events, git diff, │
│  + fast-path playbooks  │                │ traces, redaction)                       │
│ GitOps puller (Flux)    │◄───────────────│        ▼                                 │
└─────────────────────────┘   signed       │ Diagnosis Agent (LLM, read-only tools,   │
                              artifacts    │ runbook + incident retrieval)            │
 HOST LEVEL                                │        ▼                                 │
┌─────────────────────────┐                │ Fix Agent → PR in Git                    │
│ Rollback watchdog       │                │        ▼                                 │
│ (dead-man's switch)     │                │ Sandbox (isolated, no egress)            │
└─────────────────────────┘                │        ▼                                 │
                                           │ Validation Gate (independent verifier)   │
                                           │        ▼                                 │
                                           │ Policy/Risk Engine → rollout | escalate  │
                                           └──────────────────────────────────────────┘
```

### Components

| Component           | Responsibility                                                                                   |
| ------------------- | ------------------------------------------------------------------------------------------------ |
| Telemetry           | Metrics, logs, traces, Kubernetes events, and runtime security alerts                            |
| Local detector      | Rule-based detection (pod states, SLO burn rate), later anomaly detection                        |
| Fast-path playbooks | Deterministic fixes for known failures; work offline                                             |
| Incident manager    | Dedupes and correlates signals into one incident record                                          |
| Context builder     | Assembles evidence (logs, live vs. Git manifests, recent deploys) and redacts secrets and PII    |
| Diagnosis agent     | LLM with read-only tools; emits ranked hypotheses, evidence, confidence, and a remediation class |
| Fix agent           | Produces a patch as a Git PR with rationale; never touches the cluster                           |
| Sandbox             | Ephemeral, isolated environment (vcluster or kind with gVisor), no egress, no real secrets       |
| Validation gate     | Lint, schema, policy, build, tests, image scan, reproduce-then-fix, blast-radius limits          |
| Policy/risk engine  | Scores each change and maps it to an autonomy level                                              |
| Rollout controller  | Canary then waves with automatic rollback on SLO regression                                      |
| Rollback watchdog   | Host-level process that reverts a change if health is not confirmed in time                      |
| Escalation          | Pages a human with a full evidence bundle                                                        |

## Zones and trust boundaries

"Edge" and "healer" are **trust and failure boundaries**, not necessarily separate machines. A component belongs on the healer side if it must keep working when the thing it watches is broken.

| Zone             | Contains                               | Notes                                                   |
| ---------------- | -------------------------------------- | ------------------------------------------------------- |
| Workload cluster | Workloads, telemetry, GitOps puller    | The "patient". Its logs are untrusted                   |
| Healer           | Incident manager through policy engine | Run in a separate cluster or as host daemons            |
| Sandbox          | Ephemeral test environments            | Isolated from both, no real secrets, no egress          |
| Host level       | Rollback watchdog                      | Outside Kubernetes so it survives an API server failure |

On a single server, the options from weakest to strongest are: (1) separate namespaces in one cluster, (2) a separate small cluster for the healer, (3) the healer running outside Kubernetes with a scoped kubeconfig. Option 1 is fine for prototyping only.

## How a fix flows

1. **Detect:** signals are correlated into a single incident.
2. **Triage:** known failures go to a playbook, novel ones go to the diagnosis agent.
3. **Diagnose:** the agent returns ranked hypotheses with evidence. Low confidence escalates immediately.
4. **Fix:** a patch is opened as a Git PR.
5. **Validate:** the sandbox reproduces the failure, applies the patch, and confirms recovery. Machine checks run independently.
6. **Decide:** the policy engine scores risk and selects an autonomy level.
7. **Roll out:** canary, then waves, with automatic rollback. Or **escalate** with a full evidence bundle.

Every incident has a retry budget to prevent fix loops.

## Autonomy levels

| Level | Behavior                                             |
| ----- | ---------------------------------------------------- |
| L0    | Notify only                                          |
| L1    | Open a PR for human approval                         |
| L2    | Auto-apply low-risk fixes to non-critical workloads  |
| L3    | Auto-apply broader fixes within pre-approved classes |

New fix classes start at L1. Promotion requires meeting fix-rate and false-fix-rate thresholds on the eval harness.

## Repository layout

```
.
├── infra/          # Cluster bootstrap, Flux config, identities and RBAC
├── apps/           # Demo workloads with known weaknesses (test patients)
├── agent/          # Detector, incident manager (Phase 3), fast-path runner (Phase 4); later context builder, diagnosis, fix agent
├── playbooks/      # Deterministic fast-path playbooks (Phase 4)
├── policy/         # OPA/Kyverno policies, risk scoring, autonomy rules
├── watchdog/       # Host-level rollback watchdog
├── evals/          # Failure scenarios, injectors, scoring
└── docs/           # Design notes and runbooks
```

## Getting started

This is the Phase 0 lab: a local cluster where a Git commit changes the cluster, with three scoped identities. Full detail is in [docs/phase0.md](docs/phase0.md) and [docs/identities.md](docs/identities.md).

### Prerequisites

- Windows, macOS or Linux with Docker Desktop and its Kubernetes enabled (context `docker-desktop`). Planned: a k3s VM before Phase 5.
- `kubectl`, `flux` (CLI), `kustomize`, `git`, `bash` (Git Bash on Windows). `make` is not needed.
- A Git remote Flux can pull from. This repo is public, so no credential is needed.

```bash
bash scripts/check-tools.sh
```

### 1. Prepare the cluster and images

```bash
bash scripts/up.sh              # checks the context, creates namespaces
bash scripts/build-images.sh    # builds edgeloop/<app>:v1 locally (no registry)
```

### 2. Install Flux and the identities

```bash
flux install                    # controllers only; no `flux bootstrap`, so Flux has no Git write access
kubectl apply -f infra/rbac/    # diagnoser, fixer, deployer
kubectl apply -f infra/flux/    # GitRepository + one Kustomization per demo app
flux get kustomizations         # all four should become Ready
kubectl -n apps get pods
```

`flux check` reports one failing item (Kubernetes v1.34 vs Flux 2.10 wanting >= 1.35). This is expected; see [docs/identities.md](docs/identities.md#kubernetes-134-vs-flux-210).

The demo apps (`apps/oom-app`, `badconfig-app`, `crash-app`, `healthy-app`) each have a `healthy` and a `broken` overlay. Flux deploys the healthy ones; point a Kustomization at a broken overlay (via Git) to reproduce the failure. `healthy-app`'s broken overlay is a decoy that must stay Ready.

### 3. Identities

| Identity    | Permissions                                                         |
| ----------- | ------------------------------------------------------------------- |
| `diagnoser` | Read-only (pods, logs, events, deployments, ...) in namespace `apps` |
| `fixer`     | No cluster access; Git write only, via an off-cluster credential    |
| `deployer`  | Used only by Flux, scoped to workload kinds in `apps`               |

```bash
bash scripts/make-kubeconfig.sh diagnoser     # writes kubeconfigs/diagnoser.kubeconfig (1h token, gitignored)
kubectl --kubeconfig kubeconfigs/diagnoser.kubeconfig -n apps get pods                  # succeeds
kubectl --kubeconfig kubeconfigs/diagnoser.kubeconfig -n apps delete pod <name>         # Forbidden
kubectl --kubeconfig kubeconfigs/diagnoser.kubeconfig -n apps get secrets               # Forbidden
```

### 4. Verify Phase 0

```bash
bash scripts/verify-phase0.sh          # RBAC and drift-revert checks, no Git writes
bash scripts/verify-phase0.sh --git    # also commits and pushes test changes to main, then reverts them
```

### Next

Phase 1 adds observability (Prometheus, a log pipeline, OpenTelemetry). Break each demo app and confirm the failure is visible.

## Failure catalog and evaluation

The eval harness is the main way to measure progress. It lives in `evals/`.

Each scenario defines:

- an **injector** that causes the failure (Chaos Mesh, Litmus, or a script)
- an **expected diagnosis**
- a **recovery check** that decides pass or fail

Initial scenarios: bad image tag, OOM kill, wrong environment variable, failing probe, bad config rollout, dependency outage.

```bash
# Planned for Phase 2 (not implemented yet; entry points will be scripts, not make):
#   inject a scenario, wait, report
#   run the full catalog
```

Tracked metrics:

| Metric             | Meaning                                                   |
| ------------------ | --------------------------------------------------------- |
| Fix rate           | Share of scenarios the system recovers                    |
| False-fix rate     | Share of changes that were wrong, harmful, or unnecessary |
| Time to recovery   | From first signal to confirmed healthy                    |
| Diagnosis accuracy | Match against the expected diagnosis                      |

## Safety model

- **Least privilege:** diagnoser is read-only, fixer writes only to Git, deployer is GitOps-only.
- **Validation gate:** schema and lint (kubeconform, kube-linter), policy (OPA or Kyverno), build and tests, image scan and signing (Trivy, cosign), reproduce-then-fix, and diff-size limits.
- **Blast-radius limits:** by default, patches may not touch RBAC, network policy, or persistent volumes.
- **Progressive rollout:** canary on one site, then waves, with automatic rollback on SLO regression (Argo Rollouts or Flagger).
- **Dead-man's switch:** the host-level watchdog reverts any change not confirmed healthy within a timeout.
- **Prompt-injection defense:** structured tool I/O, and no instruction-following from log text.
- **Controls:** rate limits, change-freeze windows, per-incident retry budgets, a global kill switch, signed commits and images, and an immutable audit log.

## Roadmap

| Phase | Focus                                                | Exit criterion                           |
| ----- | ---------------------------------------------------- | ---------------------------------------- |
| 0     | Lab setup, GitOps, scoped identities                 | A Git commit changes the cluster         |
| 1     | Observability                                        | Every injected failure is visible        |
| 2     | Failure catalog and eval harness                     | One command injects and scores a failure |
| 3     | Detection and incident manager                       | One incident per failure                 |
| 4     | Deterministic fast path                              | Known failures self-heal via PR          |
| 5     | Safety rails: validation, sandbox, rollout, watchdog | Bad patches are blocked or reverted      |
| 6     | Context builder and diagnosis agent (read-only)      | Measured diagnosis accuracy (built; first LLM run pending) |
| 7     | Fix agent (manifests and config, at L1)              | Low false-fix rate on evals              |
| 8     | Escalation and learning loop                         | Rejected fixes are not re-proposed       |
| 9     | Hardening and graduated autonomy                     | Fix classes promoted by metrics          |

Later: code patches (always human-reviewed first), a small local model for offline triage, and multi-site canary waves.

**Tip:** Phase 4 already delivers real value at low risk. Do not skip Phase 2; without the eval harness you cannot tell whether the LLM phases help.

## Contributing

1. Open an issue describing the failure class or component you want to work on.
2. Add or update an eval scenario that covers the change.
3. Keep patches to manifests and config unless discussed first.
4. All changes need passing validation checks and one review.

## License

Choose a license (for example Apache-2.0 or MIT) and add a `LICENSE` file.
