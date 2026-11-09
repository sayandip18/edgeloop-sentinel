# agent

Phase 3 (built): rule-based detector and incident manager. Read-only. See [docs/phase3.md](../docs/phase3.md).
Phase 4 (built): deterministic fast path, incident to playbook to pull request. See [docs/phase4.md](../docs/phase4.md).

| File | Role |
| --- | --- |
| `detector.py` | Pure rules: raw Kubernetes JSON in, signals out. No I/O, no log text |
| `incidents.py` | Dedupes and correlates signals into one incident per failure; JSONL log |
| `collector.py` | The only code that talks to the cluster; diagnoser kubeconfig, refuses write-capable identities |
| `watch.py` | The loop: `python agent/watch.py [--once]` |
| `fastpath.py` | Reads the incident log, runs playbooks, validates, opens a PR with the fixer PAT. Never merges, no cluster access |
| `github.py` | Minimal GitHub client (branch, commit, PR, read PR) and the startup preflight (token not admin, main protected) |
| `gitrepo.py` | Read-only view of the repo at a fetched ref |
| `test_detection.py`, `test_fastpath.py` | Offline tests |

Phase 6 (built): context builder and read-only LLM diagnosis agent. See [docs/phase6.md](../docs/phase6.md).

| File | Role |
| --- | --- |
| `redact.py` | Secret and PII redaction applied to everything the model sees |
| `context.py` | Evidence bundle: signals, pods, live manifests, logs, events, live-vs-Git drift, recent commits; id-addressed |
| `obsquery.py` | Allowlisted read-only Prometheus/Loki queries via the diagnoser's service-proxy route |
| `diagnose.py` | OpenAI `gpt-5` agent with read-only tools; validates output; writes `diagnosed` events to the incident log. Diagnoses only |
| `service.py` | `run_diagnosis`: bundle, agent, `diagnosed` event. One entry point for the two callers below |
| `diagnose_loop.py` | Diagnoses each new incident once (separate process from `watch.py`); bounded retries; honours the run budget |
| `responder.py` | Eval harness shim (`--responder`); `--baseline` for the rules-only comparison |
| `budget.py` | Per-run token/dollar budget shared across processes via a ledger |
| `check_live_tools.py` | Exercises every agent tool against the cluster (read-only, no LLM) |
| `test_diagnosis.py` | Offline tests with a scripted fake LLM |

Phase 7 (built): LLM fix agent (manifests and config only, L1). See [docs/phase7.md](../docs/phase7.md).

| File | Role |
| --- | --- |
| `fixagent.py` | The LLM-facing module: read-only Git tools, `submit_patch` as exact find/replace edits to one app's manifests, validation. No subprocess, no token, no cluster |
| `fix_loop.py` | Diagnosed incident -> patch -> kustomize check -> independent gate (`policy/gate.py`) -> risk engine -> PR with the fixer PAT. Retry budget, cooldown, never merges |
| `test_fixagent.py` | Offline tests with a scripted fake LLM and a fake GitHub |
