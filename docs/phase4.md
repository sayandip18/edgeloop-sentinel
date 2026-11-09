# Phase 4: Deterministic fast path

**Exit criterion:** known failures self-heal via a pull request, with no LLM.

**Status: implemented, not yet run end to end.** Offline checks pass (`bash scripts/verify-phase4.sh`): playbooks tested against the real `apps/` tree for every Phase 2 fix scenario, fake-GitHub tests of the PR flow, static safety checks. Still needed from the owner: branch protection on `main`, the fixer token, and then the `--git` run. Nothing here has been committed.

## Flow

    watch.py (diagnoser, read-only) -> incidents.jsonl -> fastpath.py -> playbook -> validate -> PR (fixer PAT) -> human merges -> Flux applies

1. `fastpath.py` waits `--settle` seconds (30) after an incident opens so late signals are in, fetches `main` anonymously over HTTPS into a private ref (`refs/sentinel/main`), and asks the playbooks for a plan.
2. The plan is validated: it may only touch `apps/<app>/`, and `kubectl kustomize` must build the app the way Flux does (path taken from `infra/flux/kustomizations.yaml`). This is a minimal gate; the real, independent gate is Phase 5.
3. One commit on a new branch `sentinel/<playbook>/<app>-<timestamp>` (git data API, parent = the commit it planned against) and one PR. The PR body has the incident, signals, rationale, the diff, the checks run, the rollback instruction, and the playbooks that declined and why.
4. A human reviews and merges (L1). The fast path never merges. After the PR closes it records `pr_merged` / `pr_closed` and deletes its own branch.

If no playbook applies, it logs `declined` and an `escalate` line once per incident. Paging a human is Phase 8.

## Playbooks

See [playbooks/README.md](../playbooks/README.md). The two shipped:

- **revert-recent-change**: restore the files the latest commit modified. Covers all seven fix scenarios in the catalog (each is injected as a changed overlay), including the dependency outage, where the incident is rooted on `backend-app`.
- **raise-memory-limit**: OOMKilled with nothing to revert. Tested offline; the catalog's OOM scenario is handled by the revert playbook first, so this one is only exercised live if you run an OOM with no recent change.

Deliberately not automated: app source code, anything outside `apps/<app>/base|overlays`, reverting a revert (that is how fix loops start), reverting when a later commit changed the same files.

## Guard rails in the code

| Rail | Where |
| --- | --- |
| Refuses to start unless the token is valid, can push, is **not a repo admin**, and GitHub reports `main` as **protected** | `github.preflight` |
| No merge call exists; only creates a new `sentinel/...` ref, never updates `main` | `github.open_fix_pr` (tested) |
| One PR per incident, then never again; at most 3 PRs per app per 24h; never a second PR while one is open | `State`, `budget_problem` |
| No cluster access: no kubeconfig, only `kubectl kustomize` (offline) | `verify-phase4.sh` static checks |
| Untrusted text (signal details come from cluster state) enters the PR body only inside a code span | `md_code` |
| App names from cluster labels are checked before use in any path | `playbooks.app_prefix` |
| Token only from env `FIXER_GITHUB_TOKEN`, never written to state or logs | `fastpath.main` |

## One-time setup (owner actions)

1. **Bot account (recommended).** Create a GitHub account for the agent and add it to the repo as a collaborator with *Write*. A write collaborator cannot bypass branch protection. If you use your own admin account the preflight refuses (override: `--allow-admin-token`, not recommended).
2. **Fine-grained PAT**, owned by that account: only this repository; permissions *Contents: read and write*, *Pull requests: read and write*; nothing else; short expiry. Export it only in the shell that runs the fast path:

       export FIXER_GITHUB_TOKEN=github_pat_...

   Never put it in the repo, in the cluster, or in the diagnoser's environment.
3. **Branch protection on `main`**: Settings > Branches > add a rule for `main`: *Require a pull request before merging*. Leave *Do not allow bypassing the above settings* **off** (or use a ruleset with yourself as bypass actor). The eval harness injects failures by pushing to `main` as you, the admin, exactly as Phases 2 and 3 do; the bot is blocked, you are not. If you want a real second reviewer, require 1 approval: the bot opens the PR, you approve and merge.
4. `bash scripts/make-kubeconfig.sh diagnoser 8h` for the detector, as in Phase 3.

## Running it

    # plan only, no token, no PR (looks at an existing incident log)
    python agent/fastpath.py plan inc-0001-oom-app --incidents agent/state/incidents.jsonl
    python agent/fastpath.py run --dry-run

    # live: detector in one terminal, fast path in another
    python agent/watch.py
    python agent/fastpath.py run

    # exit criterion
    bash scripts/verify-phase4.sh            # offline, no Git writes
    bash scripts/verify-phase4.sh --git      # all 16 scenarios, real PRs; add --manual to merge them yourself

`evals/run.py run <ids|all> --fastpath [--approve auto|manual]` implies `--detector`. The runner plays the human: with `auto` it fast-forwards `main` to the PR branch and pushes (your git credentials; GitHub then shows the PR as merged), with `manual` it waits up to `APPROVE_TIMEOUT` (900s) for you to merge on GitHub. Per scenario it scores exactly one PR on the true target app (none for the decoy) and whether the cluster recovered because of the merge. New result fields: `fastpath`, aggregate `fastpath_rate` and `median_time_to_pr_s`. `fix_rate` and `median_time_to_recovery_s` now include the PR path (with `auto` approval, so it excludes human latency). `diagnosis_accuracy` stays null: diagnosis is Phase 6.

## Caveats and open points

- **Not run end to end yet.** Offline tests use a fake GitHub and a throwaway git repo built from the real `apps/` tree. Real GitHub behaviour (fine-grained PAT permissions on the git data API, `protected` flag visibility) is untested until the `--git` run.
- **The eval fix is trivially a revert.** All injected failures are committed config changes, so revert-recent-change solves them. That proves the pipeline, not breadth; the memory playbook and Phase 6/7 cover cases with nothing to revert.
- **Revert blames the latest commit to the app.** If two commits landed before the incident it reverts only the latest; the PR says which. A human reviews it (L1).
- **Eval merge path bypasses GitHub's PR UI** (`--approve auto` pushes as you), so it does not test that protection blocks the bot. The preflight checks the flag; a direct-push attempt with the bot token is not automated, by design (it would write to `main`). Try it once by hand after setup.
- **A PR left open** blocks further PRs for that app until it is merged or closed; the incident then escalates instead of piling up PRs.
- **Flux lag:** merge to recovery is bounded by the 1m Flux interval plus rollout.
- Clock assumption: commit time versus incident time (both epoch). The 120s slack and 1h window are untuned.
- `--approve auto` leaves PRs that GitHub marks merged; the fast path deletes the branches it created.
