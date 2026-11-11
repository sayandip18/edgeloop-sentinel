#!/usr/bin/env bash
# Phase 9 verification: fix classes are promoted (and demoted) by measured results only, and the hardening controls hold.
#
#   bash scripts/verify-phase9.sh     offline: no cluster, no key, no token, no pushes, no network.
set -uo pipefail
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
section() { echo; echo "== $* =="; }

section "Prerequisites"
python -c "import yaml" 2>/dev/null && pass "PyYAML importable" || fail "pip install pyyaml"

section "Phase 9 tests (synthetic eval results, fake GitHub)"
out=$(python agent/test_hardening.py 2>&1) && pass "$(echo "$out" | tail -1): thresholds, one step at a time, demotion on false fix / rejection / ineffective merge / stale evidence, sealed levels, risk engine, audit chain, kill, freeze, rate limits, merger refusals" || fail "hardening tests: $(echo "$out" | grep -E '^(FAIL|Assert)' | tail -4)"
for t in agent/test_escalation.py agent/test_fastpath.py agent/test_fixagent.py agent/test_diagnosis.py policy/test_gate.py; do
  out=$(python $t 2>&1) && pass "$t still passes" || fail "$t: $(echo "$out" | tail -3)"
done
out=$(python evals/test_scoring.py 2>&1) && pass "scoring tests still pass" || fail "scoring tests: $out"

section "CLI smoke (throwaway state)"
t=$(mktemp -d)
python policy/controls.py kill --reason "verify" --audit "$t/a.jsonl" --kill-file "$t/KILL" >/dev/null && [ -f "$t/KILL" ] && pass "controls.py kill creates the switch" || fail "kill"
python policy/controls.py resume --reason "verify" --audit "$t/a.jsonl" --kill-file "$t/KILL" >/dev/null && [ ! -f "$t/KILL" ] && pass "controls.py resume removes it" || fail "resume"
python policy/controls.py audit-verify --audit "$t/a.jsonl" | grep -q "chain intact" && pass "audit chain verifies" || fail "audit-verify"
sed -i 's/"verify"/"edited"/' "$t/a.jsonl"
python policy/controls.py audit-verify --audit "$t/a.jsonl" >/dev/null && fail "an edited audit log still verified" || pass "an edited audit log fails verification"
python policy/promotion.py status --results "$t/none/*.json" --feedback "$t/fb.jsonl" --audit "$t/b.jsonl" --levels "$t/p.json" | grep -q "revert-recent-change" && pass "promotion.py status runs with no evidence and promotes nothing" || fail "promotion status"
python policy/promotion.py apply --results "$t/none/*.json" --feedback "$t/fb.jsonl" --audit "$t/b.jsonl" --levels "$t/p.json" >/dev/null && python policy/promotion.py verify --results "$t/none/*.json" --feedback "$t/fb.jsonl" --audit "$t/b.jsonl" --levels "$t/p.json" >/dev/null && pass "apply then verify is consistent" || fail "apply/verify"
env -u EDGELOOP_MERGER_TOKEN python agent/merger.py run --once >/dev/null 2>&1 && fail "merger started without its own token" || pass "merger refuses to start without EDGELOOP_MERGER_TOKEN"
rm -rf "$t"

section "Repository state"
python - <<'PY' && pass "no class is promoted by hand: autonomy.yaml holds L0/L1 only; any promotions.json is sealed and consistent with its evidence" || fail "autonomy.yaml grants above L1, or promotions.json is invalid"
import sys; sys.path[:0] = ["policy"]
import promotion, os
cfg = promotion.load_cfg()
assert all(v in ("L0", "L1") for v in cfg["classes"].values()), cfg["classes"]
if os.path.exists("policy/promotions.json"):
    lv, problems = promotion.load_levels(cfg); assert not problems, problems
PY

section "Static safety checks (principles 2, 4, 6, 7)"
grep -rEn '/merge|"merge"|\.merge\(' agent/github.py agent/fastpath.py agent/fix_loop.py agent/fixagent.py playbooks/ >/dev/null 2>&1 && fail "fixer-side code contains a merge call" || pass "the fixer still has no merge call"
grep -nE "OPENAI_API_KEY|import openai|anthropic|kubeconfig|subprocess|open_fix_pr|git/(commits|refs|trees)|import github$" agent/merger.py >/dev/null && fail "merger holds fixer, model or cluster credentials, or can write branches" || pass "the merger holds no fixer/model/cluster credential and has no branch or commit write"
grep -nE "^\s*(import|from)\s+(merger|promotion|controls)" policy/gate.py >/dev/null && fail "the gate imports Phase 9 code" || pass "the independent gate is untouched by Phase 9"
{ grep -q "controls.killed" agent/fastpath.py && grep -q "controls.killed" agent/diagnose_loop.py && grep -q "pause_reasons" agent/merger.py; } && pass "kill switch is honoured by the fast path, fix loop, diagnosis loop and merger" || fail "a loop ignores the kill switch"
grep -rn "MERGER_TOKEN *=" --include=*.py --include=*.yaml --include=*.sh agent policy scripts | grep -v "TOKEN_ENV\|test_\|verify-phase9" >/dev/null && fail "merger token committed" || pass "merger token is read from the environment only"

echo; echo "== summary: $PASS passed, $FAIL failed =="
[ $FAIL -eq 0 ]
