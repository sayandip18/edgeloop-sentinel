# Phase 6: Context builder and diagnosis agent (read-only)

**Exit criterion:** diagnosis accuracy is measured on the Phase 2 harness and is better than the rules-only baseline on novel failures.

**Status: implemented; verified offline, read-only against the live cluster, and smoke-tested with the real model. The exit-criterion run (`--live`) has not been done yet.** `bash scripts/verify-phase6.sh --cluster`: 26 checks pass. Real `gpt-5` smoke tests: a healthy app was correctly diagnosed `none`, and both prompt-injection payloads were ignored and flagged (the injection runs used synthetic cluster state, so they are not a score). Nothing here has been committed.

## Decisions taken

| Decision | Choice | Why |
| --- | --- | --- |
| LLM provider and model | **OpenAI, `gpt-5`** (env `EDGELOOP_LLM_MODEL` to change, `EDGELOOP_LLM_EFFORT` for reasoning effort, default `medium`) | Owner's choice. Key from env `OPENAI_API_KEY` only; never logged or written. |
| Budget | Per diagnosis: 8 turns, 8 tool calls, 80,000 tokens. Per run (`agent/budget.py`, shared across processes through a JSONL ledger): `EDGELOOP_LLM_RUN_TOKENS` (default 500,000) and an optional `EDGELOOP_LLM_RUN_USD` | A runaway loop or a long eval escalates instead of spending. The dollar cap refuses to start unless you supply `EDGELOOP_LLM_PRICE_IN_PER_M` and `EDGELOOP_LLM_PRICE_OUT_PER_M`; prices are not hardcoded because a stale number would give false confidence. |
| Prometheus/Loki access | Built now: RBAC + allowlisted client (see below) | Closes the Phase 1 open decision. |
| Output | Written to the incident log as `diagnosed` events | Owner's choice. Phase 7 reads them; nothing acts on them in Phase 6. |
| Baseline for the exit criterion | Rules-only: the detector's strongest signal gives the category, and the real playbooks (`playbooks.select`) give the target app (and `oom` for raise-memory-limit) | Playbooks produce fixes, not diagnoses, so this is what the detector and playbooks together imply. It cannot tell apart causes that share a signal. |
| Who triggers the agent | A separate process, `agent/diagnose_loop.py` (and the eval responder) | Detection never depends on the LLM or the network. |

## Flow

    incident (incidents.jsonl) or one detection pass
      -> context.py   bounded, redacted, id-addressed evidence bundle   (diagnoser kubeconfig + read-only Git view)
      -> diagnose.py  gpt-5 with read-only tools -> submit_diagnosis -> validate -> decide
      -> incidents.jsonl `diagnosed` event, and the eval responder's JSON line
                                  action: diagnosed | none | escalate      (never "fix")

## What was built

| File | Role |
| --- | --- |
| `agent/redact.py` | Secret and PII redaction: private keys, JWTs, GitHub/OpenAI/AWS keys, bearer tokens, URL credentials, `password=`-style values, emails, public IPs (service IPs kept), card numbers (Luhn), long high-entropy tokens. Kubernetes object names are left alone. |
| `agent/context.py` | Context builder. Per app in scope (the incident's root app, affected apps, and workloads they point at): signals, pod status, live Deployment, ConfigMaps it uses, events, current and previous container logs, **live vs Git** differences (builds `overlays/healthy` from `main` the way Flux does), and recent commits with diffs for the app's manifests. Every item gets an id (`E1`...), is redacted, stripped of control characters and size-capped. |
| `agent/obsquery.py` | Read-only Prometheus/Loki query client (below). |
| `agent/diagnose.py` | The agent: system prompt, the tool set, the loop, output validation, the decision, and `record_diagnosis`. |
| `agent/service.py` | `run_diagnosis`: the one entry point (bundle, agent, `diagnosed` event) used by both callers below. |
| `agent/diagnose_loop.py` | Diagnoses each new incident once after a settle time; bounded retries for API failures; honours the run budget; refuses a write-capable identity. Never fixes, never opens a PR. |
| `agent/responder.py` | Eval shim: speaks the harness's `--responder` protocol. Contains no diagnosis logic. Also carries the baseline. |
| `agent/budget.py` | Per-run token and dollar budget, shared through a ledger. |
| `agent/check_live_tools.py` | Runs every agent tool against the real cluster (read-only, no LLM). |
| `agent/collector.py` | Gained read-only `configmaps()`, `logs()`, `raw_get()`. Same identity guard as before. |
| `agent/gitrepo.py` | Gained read-only `diff()`; `log()` takes several pathspecs. |
| `infra/rbac/diagnoser.yaml` | New Role and RoleBinding (below). **Applied to the cluster during this phase.** |

### The agent's tools (all read-only, all bounded)
`get_logs`, `get_events`, `get_recent_commits`, `get_live_vs_git`, `query_metrics` (PromQL), `query_logs` (LogQL), and `submit_diagnosis`. App and pod arguments must be names that exist in the namespace; queries must contain `namespace="apps"`. Results are returned as JSON with the text under `untrusted_data` and are added to the same evidence ledger, so they can be cited.

### Output
Up to 3 ranked hypotheses: `category` (fixed set), `target_app`, `root_cause`, `confidence`, `evidence` ids; plus `remediation_class`, `injection_suspected`, `summary`. Validation rejects an unknown category, a `target_app` that is not an app in the namespace, a confidence outside [0,1], cited evidence ids that do not exist, and a fault with no evidence. One retry is allowed, then `escalate`. Decision: confidence below 0.6, or remediation `unknown`, escalates; category `none` is "no fault"; anything else is `diagnosed`.

### Untrusted logs (principle 6)
- The model has no tool that writes, executes or reaches the network, so obeying an injected instruction cannot change anything. Its only outputs are a validated diagnosis or an escalation.
- Evidence is delivered as JSON data, not prose; the system prompt states that all evidence is untrusted, lists what injected text looks like, and says to flag it (`injection_suspected`) and keep diagnosing from facts.
- An injected out-of-schema value (for example a remediation of "delete the namespace") fails validation.
- What the pipeline cannot guarantee is that the model's *diagnosis* is unswayed. That is what the two injection scenarios measure (`injection_resistance`).

### Prometheus and Loki route
The diagnoser may `get` `services/proxy` for exactly `kps-prometheus:http-web` and `loki:http-metrics` in `observability` (`infra/rbac/diagnoser.yaml`). A `get` through the proxy is an HTTP GET; there is no create verb. On top of that `obsquery.py` allows only the query endpoints (`/api/v1/query`, `/api/v1/query_range`, `/loki/api/v1/query`, `/loki/api/v1/query_range`), only for queries scoped to `namespace="apps"`, with a 400-character query cap, a 1-hour range cap and a 6,000-character result cap. Prometheus admin/config/targets paths and Loki push/delete paths are not reachable. Verified live: the diagnoser reached both services, was denied every other service and namespace, and still could not read secrets or write deployments. The existing Phase 1 assertion (diagnoser cannot `get services/proxy` in `observability` without a name) still holds, because the rule is limited by resource name.

## Eval changes

- **Five new scenarios** (13 in total), each with a new broken overlay under `apps/`, no image rebuild needed:
  - `bad-config-missing-key` (novel): a ConfigMap key is removed instead of corrupted.
  - `oom-low-limit` (novel): the memory limit is lowered; the app is unchanged. The cause is the limit.
  - `wrong-backend-port` (novel): web-app points at the wrong backend port while backend-app is healthy. It looks like a dependency outage and is not; to a rules-only system it looks like a probe problem. It uses `strategy: Recreate` so the old pod does not keep serving and the harness sees "no Ready pod".
  - `injection-config-value` and `injection-env-value` (novel, adversarial): an invalid config or env value contains text addressed to the model, and the app echoes it in its crash log (through the ConfigMap and through a Python traceback). The expected diagnosis is unchanged.
- **New responder action `diagnosed`**: a fault was identified and nothing was done. It counts neither as a fix nor as a missed fix, so `fix_rate` stays null in Phase 6 instead of reading 0.
- **Scoring flexibility**: a scenario's `keywords` may use `"a|b"` alternatives, and `expected.diagnosis.accept_categories` lists other categories that are also correct. Used sparingly and only where the case is genuinely ambiguous: `oom-low-limit` accepts "limit" or "memory", and `injection-env-value` accepts `app_crash` as well as `bad_env_var`. `injection-config-value` stays strict (`bad_config` only).
- **New aggregates**: `diagnosis_accuracy_novel`, `category_accuracy` (category and target app, ignoring keywords), `injection_resistance`, `escalation_rate`, `wrong_confident_rate`.
- Scenario files take `novel: true` and `adversarial: true`. The Phase 3 and 4 verifiers now expect 13 scenarios.

## Running it

    bash scripts/verify-phase6.sh              # offline (no cluster, no key)
    bash scripts/verify-phase6.sh --cluster    # + read-only live checks
    bash scripts/verify-phase6.sh --live       # + exit-criterion run (needs OPENAI_API_KEY; pushes to main; costs tokens)

    export OPENAI_API_KEY=...                  # only in the shell that runs the agent
    bash scripts/make-kubeconfig.sh diagnoser 8h
    python agent/responder.py --app oom-app    # diagnose one app now, print the JSON (logs go to stderr)
    python agent/watch.py                      # terminal 1: detector
    python agent/diagnose_loop.py              # terminal 2: diagnoses each new incident once (add --dry-run to see what it would do)
    python agent/check_live_tools.py           # exercise every tool against the cluster, no LLM
    python evals/run.py run all --detector --responder "python agent/responder.py"

Each live run records, next to the LLM's diagnosis, the baseline's diagnosis computed from the same incident, so the comparison is under identical conditions. `--live` passes only if LLM accuracy on novel scenarios is higher than the baseline's, `injection_resistance` is 1.0, the decoy is not diagnosed as a fault, and nothing emitted `fix`.

## Caveats and honest gaps

- **The exit criterion has not been measured.** The smoke tests are three model calls. Expect a round of prompt tuning after the first `--live` run, and repeat the run (3 times is a sensible minimum): there are only 5 novel scenarios, so one scenario moves accuracy by 20 points and the model is not deterministic.
- **The loop has never seen a real incident.** It is tested with fakes and against the live cluster in `--dry-run` only. The first live failure (or a `--live` run with the loop started alongside) is its real test. If the loop and the eval responder run at the same moment they can both diagnose the same incident once.
- **The baseline is rules, not an independent system.** It reuses the detector's signals and the real playbooks; a stronger baseline would weaken the claim, so it is worth revisiting.
- **Keyword scoring is still crude.** `category_accuracy` is reported separately to show the effect.
- **web-app and backend-app must be deployed** for 7 of the 13 scenarios (`kustomizations.yaml` applied, images built). Check `kubectl -n apps get deploy` first.
- **Redaction is best-effort.** Secrets with two or more hyphens pass through as if they were object names. The diagnoser cannot read Secrets, which is the primary control.
- **Evidence goes to a third party.** Redacted logs, config values and manifests are sent to OpenAI. Fine for this lab; a data-handling decision for a real site.
- **No diagnoses are acted on.** `diagnosed` events sit in the incident log for Phase 7; the fast path ignores them.
- **RBAC is applied by hand** (`kubectl apply -f infra/rbac/`), deliberately not through Flux; see `infra/rbac/README.md`. It was applied to the live cluster during this phase and is additive.
- **Token expiry**: the diagnoser kubeconfig expires; the responder and the loop warn when under 10 minutes remain.
