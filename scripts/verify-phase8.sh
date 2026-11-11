#!/usr/bin/env bash
# Phase 8 verification: a human is paged with a full evidence bundle when nothing automated will fix an incident, and a
# fix a human rejected is not proposed again for the same incident signature.
#
#   bash scripts/verify-phase8.sh     offline: no cluster, no key, no token, no pushes, no network.
#
# The exit criterion ("a rejected fix is not re-proposed") is proven end to end against the real fast-path and fix loops
# with a fake GitHub: open PR -> human closes it -> same failure returns as a new incident -> no PR, no model call, a page.
set -uo pipefail
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
section() { echo; echo "== $* =="; }

section "Prerequisites"
python -c "import yaml" 2>/dev/null && pass "PyYAML importable" || fail "pip install pyyaml"
command -v kubectl >/dev/null && pass "kubectl found (kustomize build only)" || fail "kubectl missing"

section "Phase 8 tests (fake GitHub, scripted fake LLM, throwaway git repo built from the real apps/)"
out=$(python agent/test_escalation.py 2>&1) && pass "$(echo "$out" | tail -1): signatures, rejection memory, both fixers refuse a rejected class, escalation reasons, redaction, webhook" || fail "escalation tests: $(echo "$out" | grep -E '^(FAIL|Trace|Assert|  File)' | tail -4)"
out=$(python agent/test_fastpath.py 2>&1) && pass "fast-path tests still pass" || fail "fast-path tests: $(echo "$out" | tail -3)"
out=$(python agent/test_fixagent.py 2>&1) && pass "fix-agent tests still pass" || fail "fix-agent tests: $(echo "$out" | tail -3)"
out=$(python agent/test_diagnosis.py 2>&1) && pass "diagnosis tests still pass" || fail "diagnosis tests: $(echo "$out" | tail -3)"
out=$(python evals/test_scoring.py 2>&1) && pass "scoring tests still pass" || fail "scoring tests: $out"
out=$(python evals/run.py validate 2>&1) && pass "$out" || fail "scenario validation: $out"

section "CLI smoke (throwaway state directory)"
t=$(mktemp -d)
python agent/feedback.py reject "oom-app:oom_killed" revert-recent-change --reason "wrong fix" --feedback "$t/fb.jsonl" >/dev/null \
  && python agent/feedback.py list --feedback "$t/fb.jsonl" | grep -q "1 standing rejection" && pass "feedback.py reject then list shows the standing rejection" || fail "feedback.py reject/list"
python agent/feedback.py forget "oom-app:oom_killed" revert-recent-change --feedback "$t/fb.jsonl" >/dev/null \
  && python agent/feedback.py list --feedback "$t/fb.jsonl" | grep -q "0 standing rejection" && pass "feedback.py forget clears it" || fail "feedback.py forget"
python agent/escalate.py run --once --incidents "$t/none.jsonl" --fastpath-state "$t/fp.jsonl" --fixloop-state "$t/fl.jsonl" --out-dir "$t/esc" --feedback "$t/fb.jsonl" >/dev/null 2>&1 \
  && python agent/escalate.py list --incidents "$t/none.jsonl" --out-dir "$t/esc" --feedback "$t/fb.jsonl" | grep -q "0 escalation" && pass "escalate.py runs on an empty log and pages nobody" || fail "escalate.py run/list"
EDGELOOP_ESCALATION_WEBHOOK="http://evil.example/x" python agent/escalate.py run --once --incidents "$t/none.jsonl" --out-dir "$t/esc" >/dev/null 2>&1 \
  && fail "escalate.py accepted a plain-http, non-local webhook URL" || pass "escalate.py refuses a plain-http, non-local webhook URL"
EDGELOOP_SMTP_HOST=smtp.example.com python agent/escalate.py run --once --incidents "$t/none.jsonl" --out-dir "$t/esc" >/dev/null 2>&1   && fail "escalate.py started with half-configured email" || pass "escalate.py refuses half-configured email (no silent paging gap)"
EDGELOOP_SMTP_HOST=smtp.example.com EDGELOOP_EMAIL_FROM=s@example.com EDGELOOP_EMAIL_TO=a@example.com EDGELOOP_SMTP_SECURITY=none python agent/escalate.py run --once --incidents "$t/none.jsonl" --out-dir "$t/esc" >/dev/null 2>&1   && fail "escalate.py accepted plaintext SMTP" || pass "escalate.py has no plaintext SMTP mode"
rm -rf "$t"

section "Static safety checks (principles 2, 4, 6, 7)"
grep -qE "FIXER_GITHUB_TOKEN|OPENAI_API_KEY|import github|from github|collector|kubeconfig|subprocess|anthropic|openai" agent/escalate.py agent/feedback.py \
  && fail "escalate.py / feedback.py hold a token, a cluster client, a GitHub client, an LLM client or run subprocesses" \
  || pass "the escalator and the feedback store hold no token, no GitHub or cluster client, no LLM client and run no subprocess"
grep -rEn '/merge|"merge"|\.merge\(' agent/escalate.py agent/feedback.py >/dev/null 2>&1 && fail "merge call found" || pass "no merge call anywhere in the Phase 8 code"
grep -q "feedback" agent/fastpath.py && grep -q "rejected_before" agent/fix_loop.py && pass "both fixers consult the feedback store before proposing" || fail "a fixer does not consult the feedback store"
grep -nE "^\s*(import|from)\s+(escalate|feedback)" policy/gate.py >/dev/null && fail "the gate imports Phase 8 code" || pass "the independent gate is untouched by Phase 8 (imports nothing from it)"
grep -rn "SMTP_PASSWORD *=\|EMAIL_TO *=" --include=*.py --include=*.yaml agent policy playbooks evals | grep -v "test_" >/dev/null 2>&1 && fail "email credentials committed" || pass "SMTP credentials are read from the environment only"
grep -rn "WEBHOOK.*=\s*[\"']https\?://" --include=*.py --include=*.yaml agent policy playbooks evals >/dev/null 2>&1 && fail "a webhook URL is committed" || pass "the webhook URL is read from the environment only"

echo; echo "== summary: $PASS passed, $FAIL failed =="
[ $FAIL -eq 0 ]
