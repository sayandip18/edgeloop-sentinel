# edgeloop-sentinel: Architecture

A guided tour for someone who has never seen this codebase.

## 1. What is this?

edgeloop-sentinel is a **self-healing agent for Kubernetes services**. It watches a set of small demo apps running in a cluster. When one breaks (crashes, runs out of memory, gets a bad config), it works out why and proposes a fix as a **GitHub pull request**. A human merges the PR, or, for fix types that have earned trust, a separate "merger" does. A separate watchdog reverts any change that does not leave the app healthy.

The one idea to hold on to:

> **The agents never touch the cluster.** The only way a change reaches the cluster is a commit to `main` on GitHub, which Flux (a GitOps tool) applies. Every fix is therefore a reviewable, revertable Git change.

## 2. Vocabulary

| Term | Meaning |
| --- | --- |
| **GitOps** | The cluster's desired state lives in Git. A controller makes the cluster match Git. |
| **Flux** | The GitOps controller inside the cluster. It pulls `main` every minute and applies `apps/<app>/overlays/healthy`. |
| **Kustomize base/overlay** | Each app has a `base/` (shared manifests) and `overlays/` (variants). The `healthy` overlay is what Flux deploys. Other overlays (`broken`, `low-limit`, ...) are used to inject failures for testing. |
| **Signal** | One observed symptom, such as "oom-app's container was OOMKilled". |
| **Incident** | A deduplicated, correlated group of signals about one failure. One failure gives one incident. |
| **Playbook** | A deterministic, no-LLM recipe that turns an incident into a file change. |
| **Gate** | An independent validator that judges a proposed change. It shares no code with whatever wrote the change. |
| **Autonomy level (L0 to L3)** | How much a fix class may do alone. L0 notifies only. L1 opens a PR for a human. L2/L3 let the merger merge after a delay. |
| **Fix class** | A kind of fix, such as `raise-memory-limit` or `llm-fix-env-var`. Trust is earned per class. |

## 3. The big picture

The diagram below is the system on one page. Sections 4 to 6 walk through it.

```
CLUSTER (apps ns)                     HEALER PROCESSES (host)                        GITHUB            FLUX
─────────────────                     ─────────────────────                          ──────            ────
pods/deploys/events ──kubectl──► watch.py ──► incidents.jsonl
 (diagnoser, read-only)           detector + incident manager
                                            │
                              ┌─────────────┴───────────────┐
                              ▼                             ▼
                        fastpath.py                   diagnose_loop.py (LLM, read-only)
                        playbooks (no LLM)            context → diagnose → "diagnosed" event
                              │ declined                    │
                              │◄────────────────────────────┤
                              ▼                             │
                        fix_loop.py (LLM fixer) ◄───────────┘
                              │ plan
                              ▼
                        policy/gate.py → policy/risk.py ──► PR ──────────────► main ──► reconcile
                                                              │  (fixer token)          ▲
                        escalate.py ◄── anything unfixable    ▼                         │
                        (evidence bundle, webhook)      merger.py (L2/L3 only,   watchdog.py reverts
                                                         merge-only token)       if not healthy
```

Reading it left to right:

1. **Cluster:** the apps run here (namespace `apps`). The healer only reads from it.
2. **Healer processes:** a set of independent Python programs on the host. They communicate only through append-only JSONL files, never by calling each other.
3. **GitHub:** the only write path. Fixes land here as PRs.
4. **Flux:** sees the new commit on `main` and reconciles the cluster to it, which closes the loop.

## 4. Life of an incident, step by step

### Step 1. Detect (`agent/watch.py`, `detector.py`, `incidents.py`, `collector.py`)

- `collector.py` is the **only code that talks to the cluster**. It uses `kubectl` with the read-only `diagnoser` identity and refuses to start if that identity can write.
- `detector.py` is pure rules: raw Kubernetes JSON in, signals out (`oom_killed`, `crash_loop`, `image_pull_error`, `config_error`, `not_ready`, `dependency_down`, ...). It deliberately **ignores log text**, because logs are untrusted input.
- `incidents.py` dedupes and correlates signals. If `web-app` is down because `backend-app` is down, both land in one incident rooted at `backend-app`. Every change is appended to `agent/state/incidents.jsonl`. An incident resolves after 45 seconds with no signals.

### Step 2a. Fast path (`agent/fastpath.py`, `playbooks/`), no LLM

For known failures this reads the incident log and runs deterministic playbooks:

- `revert_recent_change`: if a recent commit probably broke the app, revert it.
- `raise_memory_limit`: if the container was OOM-killed, raise its memory limit by a bounded amount.

A playbook returns a **Plan** (exact new file contents) or declines. This path is cheap, fast, works offline and needs no model.

### Step 2b. Diagnose (`agent/diagnose_loop.py`, `context.py`, `diagnose.py`, `redact.py`), LLM, read-only

Runs in a **separate process** so detection never depends on the LLM or network. For each new incident it:

1. Builds an **evidence bundle** (`context.py`): signals, pods, live manifests, logs, events, drift between live state and Git, and recent commits. `redact.py` strips secrets and PII first.
2. Lets an LLM (with read-only tools, including allowlisted Prometheus/Loki queries via `obsquery.py`) produce a diagnosis: ranked hypotheses, confidence and a **remediation class**.
3. Appends a `diagnosed` event to the incident log. A diagnosis of "no fault" is valid: the system correctly does nothing.

`budget.py` caps token and dollar spend. Once the budget is exhausted, incidents are escalated instead of sent to the model.

### Step 3. LLM fixer (`agent/fix_loop.py`, `fixagent.py`)

Playbooks come first (cheap and deterministic first). If the fast path **declined**, and the diagnosis is a fixable fault, the LLM fixer proposes a patch. Its constraints:

- Output is limited to exact find/replace edits on **one app's manifests** (`apps/<app>/{base,overlays}`).
- It has no subprocess, no cluster access and no merge ability.
- It gets at most 2 attempts per incident. The second attempt sees why the first was rejected.
- Limits: one open PR per app, a daily cap, and a cooldown after one of its fixes merges.
- `feedback.py` remembers human rejections, so a fix a human already closed is not proposed again.

### Step 4. Validate and score (`policy/gate.py`, `policy/risk.py`)

Every plan, whether from a playbook or the LLM, goes through two checks that the author cannot influence:

- **Gate** (`policy/gate.py`), run as a separate process that imports nothing from `agent/` or `playbooks/`:
  - scope: one app, small diff, only app manifests
  - blast radius: no RBAC, NetworkPolicy, Secret, PVC, Namespace, or cross-app references
  - the app still builds with kustomize exactly as Flux would build it
  - schema (kubeconform), lint (kube-linter, new findings only) and pod-security/resource rules from `policy/rules.yaml`
  - optionally a **sandbox** (`policy/sandbox.py`): reproduce the failure in a throwaway vcluster, apply the patch and confirm recovery
- **Risk engine** (`policy/risk.py`): scores the change (lines changed, touches shared `base/`, app criticality, large memory jumps, deletions) and takes the lower of the class's autonomy ceiling and what the score allows. A failed gate means `blocked`.

### Step 5. Open a PR (fixer identity)

If the gate passes, the fixer opens a GitHub PR using its own token, `FIXER_GITHUB_TOKEN`. That token can push a branch and open a PR and **cannot merge**. `main` is branch-protected, and the tools refuse to start unless GitHub confirms the token is not admin and `main` is protected.

### Step 6. Merge

- **Default (L1):** a human reviews and merges.
- **Promoted classes (L2/L3):** `agent/merger.py` may merge. It uses a different identity (`EDGELOOP_MERGER_TOKEN`) that can only merge. It re-checks eight conditions on every pass, including kill switch off, class currently promoted, gate verdict `pass`, only allowed files touched, commits signature-verified, no human objection and the soak delay elapsed. The merge call pins the head SHA.

### Step 7. Deploy and verify (Flux and `watchdog/watchdog.py`)

- Flux notices the new commit on `main` and reconciles the cluster, using the narrow `deployer` identity.
- `watchdog.py` is a **dead-man's switch** running on the host, outside Kubernetes. For every commit titled `sentinel(<app>)...` that lands on `main`, it waits for the app to become healthy and stay healthy. If it does not within the timeout, or the app turns unhealthy, the watchdog runs `git revert` and pushes to `main` with the host's own credentials. Silence counts as failure. Subjects starting with "Revert" are never reverted again, so there are no revert loops.

### Step 8. Escalate (`agent/escalate.py`)

When no automated path remains, a human is paged with an evidence bundle (on disk, plus email or webhook). Each reason fires once per incident:

| Reason | Meaning |
| --- | --- |
| `diagnosis-escalated` | Diagnosis was low-confidence, errored or out of budget |
| `no-automated-fix` | Fast path declined and the LLM fixer cannot or will not act |
| `fix-rejected` | A human closed our PR |
| `fix-ineffective` | Our PR merged and the incident is still open |
| `pr-stale` | A PR has waited too long for review |
| `unattended` | An incident has been open a long time with no PR (the loops may be down) |

`escalate.py` is read-only with respect to the world: no cluster, no GitHub token, no LLM.

## 5. Identities: who can do what

Each role has its own credential, and none can do another's job. A compromise of one is bounded.

| Identity | Credential | Can | Cannot |
| --- | --- | --- | --- |
| **diagnoser** | Read-only kubeconfig | Read pods, logs, events, deployments in `apps`; query Prometheus and Loki | Write anything, read Secrets, exec |
| **fixer** | GitHub fine-grained PAT | Push a branch, open a PR | Merge, touch the cluster |
| **merger** | Separate merge-only token | Merge a PR the fixer opened and the gate passed | Create branches or commits, touch the cluster, call a model |
| **deployer** | Flux impersonation | Apply workloads in `apps` | RBAC, Secrets, other namespaces |
| **watchdog** | The host's own git credentials | Revert a sentinel commit on `main` | Anything else |

Definitions are in [infra/rbac/](infra/rbac/) and explained in [docs/identities.md](docs/identities.md).

## 6. Graduated autonomy and safety controls (`policy/`)

- Every fix class **starts at L1** (a human merges). `policy/autonomy.yaml` cannot grant more than L1.
- `policy/promotion.py` promotes or demotes a class from **measured evidence only**: live eval trials, human approvals and rejections, and the outcomes of earlier auto-merges. The result is a sealed `promotions.json` that `risk.py` re-validates on every call. Some classes (image tags, dependency changes) are never promoted.
- `policy/controls.py` provides the **kill switch** (the file `agent/state/KILL`), change-freeze windows, merge rate limits, and a hash-chained, tamper-evident **audit log**.

## 7. Design principles (why it is built this way)

1. **Safety rails before intelligence.** Validation, sandboxing and rollback exist before any LLM output may pass through them.
2. **GitOps is the only write path.** Agents never mutate the cluster.
3. **Cheap and deterministic first.** Playbooks run before the LLM.
4. **Separate roles, separate identities.** See section 5.
5. **Independent verification.** The gate shares no code with the authors.
6. **Logs are untrusted input.** The detector ignores log text, redaction runs before the model sees anything, and the injection scenarios in `evals/` test this.
7. **Graduated autonomy.** Trust is earned per fix class from measured results.

## 8. Repository map

| Path | What lives there |
| --- | --- |
| `agent/` | The healer processes: `watch`, `fastpath`, `diagnose_loop`, `fix_loop`, `escalate`, `merger`, plus helpers (`github`, `gitrepo`, `redact`, `budget`, `feedback`) and offline tests |
| `playbooks/` | Deterministic fix recipes (`raise_memory_limit`, `revert_recent_change`) |
| `policy/` | The gate, risk engine, sandbox, promotion engine, kill switch and audit log, plus `autonomy.yaml` and `rules.yaml` |
| `watchdog/` | The host-level rollback dead-man's switch |
| `apps/` | Demo workloads: `healthy-app`, `oom-app`, `crash-app`, `badconfig-app`, `web-app`, `backend-app`. Each has `base/`, `overlays/` and a tiny `app/` |
| `infra/` | Cluster setup: Flux config, RBAC identities, the observability stack (Prometheus, Loki, Alloy) |
| `evals/` | Scenarios that inject failures (including decoys and prompt-injection cases) and score the system |
| `scripts/` | Bring-up and tear-down, demo scripts, per-phase verification |
| `docs/` | Per-phase design docs (`phase0.md` to `phase9.md`), identities and the roadmap |

## 9. How the processes coordinate

There is no message bus. Each process is a standalone loop that reads and appends to JSONL files under `agent/state/`:

- `incidents.jsonl`: written by `watch.py` (incident events) and `diagnose_loop.py` (`diagnosed` events), read by everything else.
- Each fixer keeps its own state file. The merger and escalator read these to know what happened.
- An audit log records merges and promotions.

This keeps the processes decoupled. If the LLM or network is down, detection and the fast path still work. If a process crashes, it replays its file on restart.

## 10. Where to read next

- [README.md](README.md): the project overview and roadmap
- [docs/identities.md](docs/identities.md): the permission model in depth
- `docs/phase3.md` to `docs/phase9.md`: the design of each stage (detect, fast path, gate and watchdog, diagnose, fix, escalate, autonomy)
- [evals/README.md](evals/README.md): how the system is measured
