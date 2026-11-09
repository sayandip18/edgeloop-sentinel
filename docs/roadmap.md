# Roadmap

Detailed plan for each phase. The one-table summary lives in the [README](../README.md#roadmap). This file adds scope, deliverables, prerequisites and how each exit criterion is verified.

Phases 7 to 9 are **outlines**. Each gets a detailed plan and a `docs/phaseN.md` when work on it starts, so this file does not drift from what was built. Items marked *(decision needed)* are open.

## Fixed constraints (apply to every phase)

- **Design principles** (README): safety rails before intelligence; GitOps is the only write path; deterministic playbooks before the LLM; separate identities (diagnoser read-only, fixer Git-only, deployer Flux-only); the verifier is independent of the patch author; logs are untrusted input; graduated autonomy.
- **Every phase ends with a script** `scripts/verify-phaseN.sh` that checks its exit criterion and reports failures plainly.
- **Cluster:** Docker Desktop Kubernetes (v1.34.1) until Phase 5, then a k3s VM.
- **Agent code** lives in `agent/`. The LLM agent arrives in Phase 6 (diagnosis) and Phase 7 (fixes).
- **No commit or push** is made without the owner's go-ahead; new software is installed only after approval.

## Phase 0: Lab, GitOps, scoped identities

**Status: done.** See [phase0.md](phase0.md) and [identities.md](identities.md).

**Exit criterion:** a Git commit changes the cluster, and the diagnoser identity is provably read-only.
**Verified by:** `scripts/verify-phase0.sh [--git]` (55 checks passed; one recovery check was fixed afterwards and has not been re-run in a full `--git` pass).

## Phase 1: Observability

**Status: implemented, awaiting push and verification.** See [phase1.md](phase1.md).

**Goal:** every injected failure is visible in metrics and logs.

**Scope**
- Metrics: Prometheus and kube-state-metrics (kube-prometheus-stack).
- Logs: Loki plus a collector (Alloy or Fluent Bit).
- Optional: OpenTelemetry collector (traces are not needed for the current demo apps).
- All of it deployed by Flux from `infra/observability/` (HelmRepository and HelmRelease objects).

**Deliverables:** manifests in `infra/observability/`, `scripts/verify-phase1.sh`, `docs/phase1.md`.

**Prerequisites**
- Install helm (approved).
- A separate identity to deploy the observability stack. It needs CRDs, ClusterRoles and DaemonSets, which the current `deployer` must not get. Keep `deployer` scoped to `apps`.
- The diagnoser's read access to Prometheus and Loki *(decision needed)*: those APIs are in-cluster services, not Kubernetes RBAC objects, so how the diagnoser reaches them without gaining write access must be decided.
- Check Docker Desktop memory. kube-prometheus-stack plus Loki is heavy; a lighter option is kube-state-metrics plus log collection only.

**Exit criterion:** break each demo app via Git and confirm the failure is visible: OOMKilled and restarts in metrics, exit code and log lines in Loki, and `healthy-app` showing nothing wrong.

## Phase 2: Failure catalog and eval harness

**Status: implemented, awaiting push and end-to-end run.** See [phase2.md](phase2.md).

**Goal:** one command injects a failure and scores the outcome.

**Scope**
- A scenario definition per failure: an **injector**, an **expected diagnosis**, and a **recovery check**.
- Initial scenarios (README): bad image tag, OOM kill, wrong environment variable, failing probe, bad config rollout, dependency outage. The four existing demo apps cover three; new broken overlays are needed for the rest.
- Injectors that go through Git (commit a broken overlay, revert afterwards), consistent with GitOps as the only write path.
- Metrics (README): fix rate, false-fix rate, time to recovery, diagnosis accuracy.
- Decoy scenarios: `healthy-app`'s broken overlay must score as "no action needed", so false fixes are measurable.

**Deliverables:** `evals/` scenarios and runner (a script, not `make`, since `make` is not installed), a results format, `docs/phase2.md`.

**Exit criterion:** one command injects and scores a failure.

**Why it cannot be skipped:** without this harness there is no way to tell whether Phases 6 and 7 help.

## Phase 3: Detection and incident manager

**Status: implemented, awaiting push and end-to-end run.** See [phase3.md](phase3.md).

**Goal:** one incident per failure.

**Scope**
- Rule-based local detector (pod states, restart counts, OOMKilled, CrashLoopBackOff, probe failures).
- Incident manager that dedupes and correlates signals into one incident record.
- Runs with the diagnoser identity only (read-only).

**Exit criterion:** every Phase 2 scenario produces exactly one incident, and the decoy produces none.
**Verified by:** `scripts/verify-phase3.sh [--git]`.

## Phase 4: Deterministic fast path

**Status: implemented, awaiting branch protection, a fixer token and the end-to-end run.** See [phase4.md](phase4.md).

**Goal:** known failures self-heal through a pull request, with no LLM.

**Scope**
- Playbooks in `playbooks/` for known classes (for example raise a memory limit after OOM, revert a bad config).
- A playbook produces a Git change as a PR (principle 2). The fixer's fine-grained PAT is first needed here or in Phase 7 *(decision needed: whether playbooks use the fixer identity)*; see [identities.md](identities.md#fixers-git-credential-phase-7-not-created-yet).
- Autonomy L1: a human approves the PR. **Branch protection on `main` must be enabled before this phase**, so the PAT cannot push directly.

**Exit criterion:** known failures self-heal via PR. The README notes that this phase already delivers real value at low risk.
**Verified by:** `scripts/verify-phase4.sh [--git]`.

## Phase 5: Safety rails

**Status: implemented, verified offline; live sandbox and watchdog runs pending.** See [phase5.md](phase5.md). k3s move deferred (not required); sandbox = vcluster; Python gate + host watchdog; Argo Rollouts/Flagger and image scanning not done.

**Goal:** bad patches are blocked or reverted.
**Verified by:** `scripts/verify-phase5.sh [--cluster]`.

**Scope**
- **Move to a k3s VM first** (planned since Phase 0; also clears the Flux 2.10 / k8s 1.34 warning).
- Sandbox: isolated, no egress, no real secrets (vcluster or kind with gVisor) *(decision needed)*.
- Validation gate, independent of the patch author: lint and schema (kubeconform, kube-linter; kubeconform is not installed), policy (OPA or Kyverno), build and tests, image scan, reproduce-then-fix, diff-size limits.
- Blast-radius rule: patches may not touch RBAC, network policy or persistent volumes.
- Progressive rollout with automatic rollback (Argo Rollouts or Flagger), and a host-level rollback watchdog (dead-man's switch) outside Kubernetes.
- Policy/risk engine mapping each change to an autonomy level.

**Exit criterion:** deliberately bad patches are blocked by the gate or reverted by the watchdog.

## Phase 6: Context builder and diagnosis agent (read-only)

**Status: implemented; verified offline and read-only against the live cluster; smoke-tested with the real model (healthy app, two injection payloads). The `--live` exit-criterion run is pending.** See [phase6.md](phase6.md). OpenAI `gpt-5`; Prometheus/Loki query route built; diagnoses written to the incident log; 5 new scenarios (3 novel, 2 prompt-injection).
**Verified by:** `scripts/verify-phase6.sh [--cluster|--live]`.

**Goal:** measured diagnosis accuracy.

**Scope**
- Context builder: logs, events, live vs Git manifests, recent deploys, with secrets and PII redacted.
- LLM diagnosis agent with read-only tools, producing ranked hypotheses, evidence, confidence and a remediation class. Low confidence escalates.
- Runs only as the `diagnoser` identity. Logs are treated as untrusted data: structured tool I/O, no instruction-following from log text. Prompt-injection tests are part of the eval set.
- Model choice and API budget *(decision needed)*.

**Exit criterion:** diagnosis accuracy is measured on the Phase 2 harness and is better than the playbook-only baseline on novel failures.

## Phase 7: Fix agent (manifests and config, at L1)

**Goal:** low false-fix rate on evals.

**Scope**
- The fix agent turns a diagnosis into a patch and opens a PR with rationale, using the fixer's fine-grained PAT (Contents and Pull requests write, one repo). It never touches the cluster.
- Every patch goes through the Phase 5 validation gate, run by a component that is not the author.
- Retry budget per incident to prevent fix loops. Manifests and config only; no code patches.

**Exit criterion:** low false-fix rate on evals, with `healthy-app` never "fixed".

## Phase 8: Escalation and learning loop

**Goal:** rejected fixes are not re-proposed.

**Scope**
- Escalation: page a human with a full evidence bundle (channel *(decision needed)*).
- Record human approvals and rejections; feed them back so a rejected fix class is not proposed again for the same incident signature.

**Exit criterion:** a rejected fix is not re-proposed.

## Phase 9: Hardening and graduated autonomy

**Goal:** fix classes are promoted by metrics.

**Scope**
- Promotion L1 to L2 to L3 per fix class, only after meeting fix-rate and false-fix-rate thresholds on the eval harness.
- Controls from the README safety model: rate limits, change-freeze windows, a global kill switch, signed commits and images, an immutable audit log.

**Exit criterion:** at least one fix class is promoted purely by measured results, and demoted if its metrics regress.

## Later (not numbered)

Code patches (always human-reviewed first), a small local model for offline triage, multi-site canary waves.

## Open decisions

| Decision | Needed by |
| --- | --- |
| How the diagnoser reaches Prometheus/Loki without write access | Phase 1 |
| Docker Desktop memory vs full kube-prometheus-stack | Phase 1 |
| ~~Whether playbooks use the fixer identity and PAT~~ decided: yes, a fine-grained PAT (phase4.md) | Phase 4 |
| Branch protection on `main` (required): **owner action still pending** | Before Phase 4 |
| ~~Sandbox technology~~ decided: vcluster | Phase 5 |
| ~~LLM provider, model and budget~~ decided: OpenAI gpt-5, hard per-diagnosis caps (phase6.md) | Phase 6 |
| Escalation channel | Phase 8 |
