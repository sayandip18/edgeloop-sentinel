# Phase 7: Fix agent (manifests and config, at L1)

**Exit criterion:** low false-fix rate on the evals, with `healthy-app` never "fixed".

**Status: implemented; verified offline (`bash scripts/verify-phase7.sh`: 15 checks pass). The `--live` exit-criterion run has not been done**, and it needs the Phase 4 owner setup (bot account, fixer PAT, branch protection on `main`). Nothing here has been committed.

## Decisions taken (defaults; tell me if you want any changed)

| Decision | Choice | Why |
| --- | --- | --- |
| Patch format | Exact find/replace **edits** to existing manifest files of one app (max 4 edits), not whole-file output | Minimal diffs, no file creation or deletion, and a `find` that does not match exactly once is rejected before anything else sees it. |
| Model | Same as Phase 6 (`gpt-5`, `EDGELOOP_LLM_MODEL`/`EDGELOOP_LLM_EFFORT`, shared `budget.py` ledger) | One budget and one provider to reason about. |
| What triggers a fix | A Phase 6 `diagnosed` event with action `diagnosed`, a fixable remediation class, and a target app equal to the incident's app or one it affects | `none`, `escalate`, low confidence and `unknown` never reach the fixer. This is the decoy protection: `healthy-app` is diagnosed `none`. |
| Playbooks vs LLM | `--fastpath-state PATH` makes the fix loop act only on incidents the deterministic fast path **declined** (principle 3). For the eval, the fixer runs alone so it is measured on every scenario | Otherwise the revert playbook solves all 12 fix scenarios and the LLM would never be exercised. |
| Fix classes | One per remediation class (`llm-fix-env-var`, `llm-raise-memory-limit`, ...), each L1 in `policy/autonomy.yaml` | Phase 9 can promote or demote each on its own results. Unlisted classes default to L0. |
| Retry budget | Per incident: 2 patch proposals (the 2nd sees why the 1st was rejected), then escalate. Per app: one open PR at a time, 3 PRs per 24h, and no new PR for 1h after one of our fixes was merged (a merged fix that did not cure it is not stacked on) | Prevents fix loops. |

## Flow

    incidents.jsonl (detector + Phase 6 `diagnosed` event)
      -> fix_loop.py  picks an actionable diagnosis, checks budgets
      -> fixagent.py  gpt-5, read-only Git tools -> submit_patch (edits) -> exact-match validation     [no token, no cluster]
      -> kustomize build must succeed and the BUILT OUTPUT must change
      -> policy/gate.py   separate process, shares no code with the author -> verdict
      -> policy/risk.py   autonomy level (blocked / L0 -> no PR, escalate)
      -> PR on a new `sentinel/llm-<class>/<app>-<ts>` branch with the fixer PAT   (L1: a human merges)
      -> Flux applies after merge; the Phase 5 watchdog reverts a merged fix that leaves the app unhealthy

## What was built

| File | Role |
| --- | --- |
| `agent/fixagent.py` | System prompt, tools (`list_files`, `read_file`, `get_recent_commits`, `render_app`, `submit_patch`, `decline_fix`), `apply_edits`, `validate_patch`, the loop. Contains no subprocess, no GitHub client, no token, no cluster code (checked by the verifier). |
| `agent/fix_loop.py` | Orchestration, retry budget, PR body. Subclasses the fast path's `FastPath`, so state, reconcile (merged/closed tracking) and branch cleanup are shared. The only external program is `kubectl kustomize` (offline). |
| `agent/gitrepo.py` | Gained `ls()`. |
| `policy/autonomy.yaml` | Seven `llm-*` fix classes at L1. |
| `evals/run.py` | `--fixagent`: starts `diagnose_loop.py` and `fix_loop.py` instead of the fast path; same PR scoring and `--approve`. Records `attempts` and `gate_blocked` per scenario. |
| `scripts/verify-phase7.sh`, `agent/test_fixagent.py` | Verification (below). |

## Guard rails

| Rail | Where |
| --- | --- |
| The model can only read manifests (base/overlays YAML) at one Git ref. No write, exec or network tool | `FixToolbox` |
| A patch may only edit existing manifests of the diagnosed target app; `find` must match exactly once; no creation or deletion; a no-op is rejected | `apply_edits` |
| Everything the model reads or is told (diagnosis text, signal details, file content, commit messages) is untrusted: redacted, capped, delivered as JSON; the prompt says never to follow it | `build_task`, `SYSTEM_PROMPT` |
| Model text reaches the PR only inside code spans; the PR title is deterministic | `llm_pr_body` |
| Independent gate (scope, blast radius, kustomize, kubeconform, kube-linter, workload policy, optional vcluster sandbox) in a separate process; `incomplete` counts as blocked | `policy/gate.py` via `run_gate` |
| The built output must differ from today's, so a cosmetic patch cannot count as a fix | `FixLoop._check` |
| Autonomy ceiling L1; no merge call exists; preflight refuses an admin token or an unprotected `main` | `risk.decide`, `github.preflight` |
| Per-run LLM token/dollar budget; per-diagnosis caps (8 turns, 8 tool calls, 80k tokens) | `budget.py`, `FixAgent` |

## Running it

    bash scripts/verify-phase7.sh              # offline: no cluster, key, token or pushes
    bash scripts/verify-phase7.sh --live       # exit criterion; needs OPENAI_API_KEY + FIXER_GITHUB_TOKEN; pushes to main; costs tokens

    export OPENAI_API_KEY=... FIXER_GITHUB_TOKEN=...      # only in the shell that runs the loops
    python agent/watch.py                                  # terminal 1: detector
    python agent/diagnose_loop.py                          # terminal 2: Phase 6 diagnosis
    python agent/fastpath.py run                           # terminal 3: playbooks first
    python agent/fix_loop.py run --fastpath-state agent/state/fastpath.jsonl    # terminal 4: LLM fixes for what playbooks declined
    python agent/fix_loop.py run --dry-run                 # propose and gate, print the diff, open nothing (no token)
    python evals/run.py run all --fixagent                 # the eval

`--live` passes when (it now also requires at least 4 decoys to have run): no decoy got a PR, `false_fix_rate` is 0, no PR targeted the wrong app and no incident got more than one PR. `fix_rate` and time to PR are reported alongside, not thresholded: with 12 fix scenarios and a non-deterministic model, repeat the run (3 times is a sensible minimum) before reading anything into one number.

## Caveats and honest gaps

- **Not run live.** The fake LLM tests prove the plumbing and the rails, not that `gpt-5` writes good patches. Expect a round of prompt tuning after the first `--live` run.
- **Decoys (added in this phase):** four now exist, all on `healthy-app` and all manifest-only (no image rebuild): `decoy-noisy-healthy` (scary ERROR logs), `decoy-lower-memory` (limit 64Mi -> 48Mi plus noise, tempting an OOM diagnosis), `decoy-lower-cpu` (CPU limit halved) and `decoy-extra-env` (noise plus a new `LOG_LEVEL` env var). The catalog is now 16 scenarios; the Phase 3 and 4 verifiers expect 16. Four decoys still make a small sample, so a 0% false-fix rate is encouraging, not proof. The protection measured is mostly the Phase 6 diagnosis (`none`) plus the gate.
- **A wrong-but-plausible patch can pass the gate.** The gate checks safety and shape, not correctness; "recovered after merge" in the eval and the human reviewer are the correctness checks. With the revert-shaped eval failures, the right patch is usually "undo the last commit", which `get_recent_commits` hands the model.
- **The retry feedback tells the author why the gate blocked.** That makes the gate something the author can iterate against (2 attempts only). It does not weaken the gate (it still decides), but it is a mild oracle.
- **The sandbox check is optional** (`fix_loop.py run --sandbox`) and slow, as in Phase 5.
- **Evidence goes to a third party** (redacted manifests and diagnosis text go to OpenAI), as in Phase 6.
- **Eval merge path** (`--approve auto`) still bypasses GitHub's PR UI, as in Phase 4.
- Cooldown (1h), retry counts and the 0.7 patch-confidence floor are untuned first guesses.
