# Phase 8: Escalation and learning loop

**Exit criterion:** a rejected fix is not re-proposed.

**Status: implemented; verified offline (`bash scripts/verify-phase8.sh`: 20 checks pass).** Not run against a live cluster or real GitHub. Nothing here has been committed.

## Decisions taken (defaults; tell me if you want any changed)

| Decision | Choice | Why |
| --- | --- | --- |
| Escalation channel (**owner decided: email**) | **Email over SMTP** (TLS mandatory, configured from env only), plus the bundle on disk (always), stdout, and an optional webhook | The email carries a sanitised summary and the full redacted bundle as plain text. Each channel has its own bounded retry budget, so a dead webhook never blocks the email. |
| What a "rejection" is (**owner decided: closing always counts**) | A PR **closed without merging**, as recorded by the loops' reconcile from GitHub's own state | Only GitHub's record of a human action counts. No text in a PR, log or model output can create or clear a decision. |
| Rejection granularity | **Signature** (`<root app>:<strongest signal type>`) x **fix family** | Coarse so the same failure matches even when a second signal joins the incident late; family so rejecting the `revert-recent-change` playbook also blocks `llm-revert-change` (re-proposing through the other door would defeat the point). A different class for the same signature is still allowed. |
| Does a rejection expire? | No. A human clears it with `feedback.py forget`, or a later merge of the same family clears it | Silent expiry would re-propose a fix a human already said no to. |
| Who pays for a blocked proposal | Nobody: the check runs **before** the model is called | A rejected class costs zero tokens. |
| Escalation trigger | State-driven, once per (incident, reason); a diagnosis of "no fault" never pages | The decoys must stay quiet. |

## Flow

    PR closed unmerged  -> loops' reconcile writes pr_closed -> feedback.ingest -> `rejected` (signature, fix family)
    new incident, same signature -> fast path / fix loop: rejected_before() -> declined (no PR, no model call)
                                    -> escalator: no-automated-fix, bundle names the standing rejection -> human paged

    incident log + fastpath.jsonl + fixloop.jsonl -> escalate.py -> escalations/<id>.json|.md -> stdout / webhook

## Escalation reasons (each pages once per incident)

| Reason | Fires when |
| --- | --- |
| `diagnosis-escalated` | The diagnosis agent was low-confidence, errored (after its retries) or hit the budget |
| `no-automated-fix` | The fast path declined and the LLM fixer declined, errored, or the diagnosis is not actionable. Includes "a human rejected this class before" |
| `fix-rejected` | A human closed our PR without merging |
| `fix-ineffective` | Our PR was merged and the incident is still open 10 minutes later |
| `pr-stale` | Our PR has waited for review more than 4 hours |
| `unattended` | Open 30 minutes with no PR and nothing else fired (a loop may be down) |

`diagnosis-escalated` suppresses `no-automated-fix` so one failure is one page. Thresholds are flags on `escalate.py run`.

## The evidence bundle

`agent/state/escalations/<id>.json` (full) and `.md` (human-readable): incident and signals, signature, diagnosis (hypotheses, remediation class, tool calls), what the fast path and fixer did (declines, attempts, gate verdicts, PR links and outcomes), standing rejections for the signature, recent commits touching the app, and next steps for that reason. All redacted with the same `redact.py` the models use.

## What was built

| File | Role |
| --- | --- |
| `agent/feedback.py` | `signature`, `family`, `FeedbackStore` (append-only, latest decision wins, idempotent `ingest`), CLI `list/ingest/reject/forget` |
| `agent/escalate.py` | `Escalator` (reasons, bundle, markdown, email + webhook with per-channel bounded retries), CLI `run/list/show/ack` |
| `agent/fastpath.py`, `agent/fix_loop.py` | Take a `feedback` store; ingest PR outcomes each pass; decline a rejected class (fix loop: before spending tokens). New `--feedback PATH` flag |
| `agent/test_escalation.py` | 13 offline tests |
| `scripts/verify-phase8.sh` | Verification |

## Guard rails

| Rail | Where |
| --- | --- |
| The escalator and the store hold no token, GitHub client, cluster client or LLM client and run no subprocess (checked by the verifier) | `escalate.py`, `feedback.py` |
| Decisions come only from `pr_closed` / `pr_merged` state events or an explicit CLI action | `FeedbackStore.ingest` |
| Untrusted text in the webhook message is redacted, capped and stripped of `@`, `<>`, backticks, markup and URLs; the webhook URL is never logged or committed | `chat_safe`, `notify` |
| Plain-http webhooks are refused except localhost. Email: TLS with certificate checks only, validated single-line addresses, plain-text body, half-configured email refuses to start, credentials and addresses never logged | `webhook_url_problem`, `email_config_from_env` |
| Paging failure never loses the escalation: the bundle is written first, retries are bounded, the row stays visible as `not-paged` | `Escalator.step` |
| The gate and the autonomy policy are untouched | `policy/` |

## Running it

    bash scripts/verify-phase8.sh                         # offline, no cluster/key/token

    export EDGELOOP_SMTP_HOST=smtp.example.com EDGELOOP_SMTP_PORT=587 EDGELOOP_SMTP_SECURITY=starttls   # or ssl (465)
    export EDGELOOP_SMTP_USER=... EDGELOOP_SMTP_PASSWORD=...                  # only in the shell that runs the escalator
    export EDGELOOP_EMAIL_FROM=sentinel@example.com EDGELOOP_EMAIL_TO=oncall@example.com,me@example.com
    python agent/escalate.py run                          # alongside the other loops; EDGELOOP_ESCALATION_WEBHOOK optional
    python agent/escalate.py list                         # open escalations
    python agent/escalate.py show esc-inc-0001-oom-app-no-automated-fix
    python agent/escalate.py ack  esc-inc-0001-oom-app-no-automated-fix --note "fixing by hand"
    python agent/feedback.py list                         # what the system has learned
    python agent/feedback.py reject oom-app:oom_killed llm-raise-memory-limit --reason "wrong approach"
    python agent/feedback.py forget oom-app:oom_killed llm-raise-memory-limit

## Caveats and honest gaps

- **Not run live.** The exit criterion is proven end to end against the real loops with a fake GitHub (open, close, recur, no PR and no model call, a page), not against real GitHub. `evals/run.py` is unchanged and has no rejection scenario yet.
- **The signature is coarse.** `oom-app:oom_killed` also blocks a *different* root cause with the same symptom if the same fix class is chosen. A human can `forget`. A finer signature (including the diagnosed category) would cut false blocks but also miss real repeats; first guess, untuned.
- **Closing a PR always counts as a rejection** (owner's call), including for unrelated reasons; `forget` undoes it.
- **A rejection carries no reason.** GitHub's closing comment is not read (that would need more token scope and feeds untrusted text back in). The CLI `reject --reason` records one for manual rejections only.
- **Approvals are recorded but not used yet.** Phase 9 reads them (with the eval results) for promotion.
- **Email and the webhook are untested against real servers**; the tests inject the transport. Send yourself one real escalation before relying on it (for a Gmail or Microsoft 365 sender, use an app password or SMTP relay account, not your login).
- **The email contains the redacted bundle**, so it goes through your mail provider. Redaction is pattern-based, as everywhere else in this project: not a guarantee.
- **`pr-stale`, `fix-ineffective` and `unattended` thresholds** (4 h, 10 min, 30 min) are first guesses.
- **Evals** still do not exercise escalation: on a failing eval scenario the escalator would page. Run it only against a real deployment.
