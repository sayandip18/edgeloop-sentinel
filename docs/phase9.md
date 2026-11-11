# Phase 9: Hardening and graduated autonomy

**Exit criterion:** at least one fix class is promoted purely by measured results, and demoted if its metrics regress.

**Status: implemented; verified offline (`bash scripts/verify-phase9.sh`: 21 checks pass).** The exit criterion is proven on synthetic eval results (`test_exit_criterion_promote_by_results_then_demote_on_regression`). **No real class is promoted yet**: that needs live `--fastpath` / `--fixagent` eval runs plus human-merged PRs. The merger has not run against real GitHub. Nothing is committed.

## Decisions taken (owner chose the first two)

| Decision | Choice |
| --- | --- |
| What L2/L3 mean | **Auto-merge by a separate merger identity** (`agent/merger.py`, token `EDGELOOP_MERGER_TOKEN`, merge-only). L2 waits a 30 min soak, L3 2 min. The fixer still cannot merge |
| Thresholds | **Strict**: L2 needs 10 trials over 3+ scenarios, fix rate >= 0.9, 0 false fixes, 3 human approvals; L3 needs 25 / 5 / 0.95 / 0 / 10. Editable in `policy/autonomy.yaml` (`promotion:`) |
| Demotion | One step down when the class stops meeting its current level: a false fix, a standing rejection, an auto-merge that did not resolve its incident, a lower fix rate, or evidence that aged out (30 day window) |
| Excluded from promotion | `llm-fix-image-tag` (signed-image verification not built), `llm-restore-dependency` (too broad) |

## How a class is promoted

    evals/results/*.json (live runs) + feedback.jsonl (human merges/closes) + audit.jsonl (earlier auto-merges)
        -> promotion.py apply -> policy/promotions.json (sealed, with its evidence) -> risk.py -> level in the PR decision
        -> merger.py merges when every condition holds

- **Evidence** per class: trials (eval records where that class opened a PR), distinct scenarios, fix rate, false fixes (a PR on a decoy), human approvals (a PR the merger merged is **not** one), standing rejections (by fix family, as in Phase 8), ineffective auto-merges.
- **One step per `apply`.** L1 to L3 takes two runs.
- **Nobody sets a level by hand.** `autonomy.yaml` cannot grant above L1 (risk.py ignores it). `promotions.json` carries a seal (SHA-256, or HMAC if `EDGELOOP_PROMOTION_KEY` is set) and its evidence; on every call risk.py re-checks each entry against the thresholds and clamps one that fails to L1. A bad seal voids the file.
- **Promoted does not mean unreviewed for big changes.** L3 also needs risk score <= 15, L2 <= 30; above that the PR goes to a human as L1.

## Controls (`policy/controls.py`, config in `autonomy.yaml` under `controls:`)

| Control | Behaviour |
| --- | --- |
| Kill switch | `python policy/controls.py kill --reason ...` (file `agent/state/KILL`, or env `EDGELOOP_KILL=1`). Stops the fast path, fix loop, diagnosis loop (no tokens spent) and merger. The escalator keeps paging. `resume` clears it. Both are audited |
| Change-freeze windows | ISO date ranges and weekly rules (UTC). Block auto-merging. A malformed rule fails closed |
| Rate limits | Auto-merges per hour (2), per day (6), per app per day (2), counted from the audit log so restarts do not reset them |
| Audit log | `agent/state/audit.jsonl`, hash-chained. Records every promotion, demotion, merge attempt (written **before** the merge call), hold, outcome and kill-switch change. `audit-verify` finds edits, deletions and reordering; the merger refuses to start on a broken chain |
| Signed commits | The merger merges only when GitHub reports every PR commit as signature-verified (`require_verified_commits`) |

## The merger

Merges only when all hold, re-checked each pass: no kill/freeze/rate-limit; class promoted now; no standing rejection; recorded gate verdict `pass` with a small score; PR is `sentinel/*` into `main`, not a draft, from the fixer account; head commit unchanged since first seen (the merge call also pins the head SHA); files read from GitHub stay under `apps/`, at most 3, none deleted or renamed; commits verified; no `hold` label or change request; soak elapsed. After a merge it checks the incident `verify_after_minutes` later and records `auto_merge_verified` or `auto_merge_ineffective` (which demotes the class).

## One-time setup (owner)

1. Create a **separate** GitHub account or fine-grained PAT for the merger (Pull requests write, one repo). Set `EDGELOOP_MERGER_TOKEN` and `EDGELOOP_FIXER_LOGIN` only in the merger's shell.
2. Branch protection on `main`: the merger must be able to merge without a human approval (0 required approvals, or the merger in the bypass list) while the fixer cannot. L1 classes are unaffected: humans still merge those.
3. Make the fixer's commits verified (see gaps), or set `require_verified_commits: false` knowingly.

## Running it

    bash scripts/verify-phase9.sh                    # offline
    python policy/promotion.py status                # what each class would get, and why (needs live eval results first)
    python policy/promotion.py apply                 # write promotions.json; commit it as the reviewed record
    python policy/promotion.py verify
    python agent/merger.py run --dry-run             # decisions only
    python agent/merger.py run
    python policy/controls.py kill --reason "..."    # stop everything automated
    python policy/controls.py audit-verify

## Caveats and honest gaps

- **Not live.** Promotion has only run on synthetic results; the merger only against a fake GitHub. Expect surprises at first contact.
- **Eval results are files.** Anyone who can write `evals/results/` can forge evidence. The seal catches edits to `promotions.json`, not forged inputs. `promotion.py verify` recomputes evidence (and flags drift, which is also normal after new runs), and a human should review the `apply` diff before committing it.
- **Signed commits are likely to block L2 at first.** The fixer creates commits through the Git Data API with a PAT, which GitHub does not necessarily mark verified. The merger fails closed. Signing the fixer's commits (an SSH/GPG key on the fixer side) is not built.
- **Signed images are not built.** Image-tag fixes are excluded from promotion instead. Nothing verifies image signatures (cosign) yet.
- **The audit log is tamper-evident, not tamper-proof.** Anchor `controls.py audit-head` outside the agent's reach and use OS append-only if available.
- **Trials are attributed to the class that opened the PR.** A class that never fires on a scenario collects no evidence there, so the fix rate is "success when it acts", not coverage. The three-scenario minimum limits the damage.
- **Rollback is not new.** A bad auto-merge is limited by the gate, the score caps, the rate limits and the soak, and caught by Flux health plus the Phase 5 watchdog; the merger itself cannot revert.
- Thresholds are first guesses and are untuned.
