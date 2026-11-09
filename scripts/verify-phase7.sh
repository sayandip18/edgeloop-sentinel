#!/usr/bin/env bash
# Phase 7 verification: the LLM fix agent has a low false-fix rate on the evals, and healthy-app is never "fixed".
#
#   bash scripts/verify-phase7.sh          offline: patch validation, tool scoping, agent loop with a scripted fake LLM, the fix loop
#                                          (retry budget, gate, cooldown, dry run), eval-harness wiring, static safety checks.
#                                          No cluster, no key, no token, no pushes.
#   bash scripts/verify-phase7.sh --live   ALSO the exit-criterion run: every scenario injected through Git (pushes to origin/main,
#                                          all reverted), detector + diagnosis loop + fix loop running, real PRs opened with the
#                                          fixer PAT. Needs FIXER_GITHUB_TOKEN, OPENAI_API_KEY, a running cluster and the Phase 4/5
#                                          one-time setup. Add --yes to skip the confirmation, --manual to merge the PRs yourself.
set -uo pipefail
cd "$(dirname "$0")/.."
LIVE=0; YES=""; APPROVE=auto
for a in "$@"; do case "$a" in --live) LIVE=1 ;; --yes) YES=--yes ;; --manual) APPROVE=manual ;; *) echo "unknown arg: $a" >&2; exit 2 ;; esac; done
PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
section() { echo; echo "== $* =="; }

section "Prerequisites"
python -c "import openai, yaml" 2>/dev/null && pass "openai and PyYAML importable" || fail "pip install openai pyyaml"
command -v kubectl >/dev/null && pass "kubectl found (kustomize build only)" || fail "kubectl missing"

section "Phase 7 unit tests (scripted fake LLM, fake GitHub, throwaway git repo built from the real apps/)"
out=$(python agent/test_fixagent.py 2>&1) && pass "$(echo "$out" | tail -1): edit rules, tool scope, agent outcomes, injection inertness, PR flow, retry budget, gate, cooldown, dry run" || fail "fix agent tests: $(echo "$out" | grep -E '^(FAIL|Trace|Assert)' | head -5)"
out=$(python agent/test_fastpath.py 2>&1) && pass "fast-path tests still pass" || fail "fast-path tests: $(echo "$out" | grep -E '^(FAIL|Trace|Assert)' | head -3)"
out=$(python agent/test_diagnosis.py 2>&1) && pass "diagnosis tests still pass" || fail "diagnosis tests: $(echo "$out" | grep -E '^(FAIL|Trace|Assert)' | head -3)"
out=$(python evals/test_scoring.py 2>&1) && pass "scoring tests still pass" || fail "scoring tests: $out"
out=$(python evals/run.py validate 2>&1) && pass "$out" || fail "scenario validation: $out"
out=$(python evals/run.py run oom-kill --fixagent --dry-run 2>&1) && echo "$out" | grep -q "LLM fix agent: on" && pass "eval runner accepts --fixagent (dry run, nothing pushed)" || fail "eval runner --fixagent: $out"

section "Static safety checks (principles 1, 2, 4, 6, 7)"
grep -qE "^\s*import subprocess|from subprocess|os\.system" agent/fixagent.py && fail "fixagent.py (the LLM-facing module) runs subprocesses" || pass "fixagent.py runs no subprocess: its only reach is the read-only GitRepo view and an injected render callable"
grep -qE "FIXER_GITHUB_TOKEN|import github|from github|collector|kubeconfig|EDGELOOP_CONTEXT|--context" agent/fixagent.py && fail "fixagent.py references the fixer token, GitHub, a cluster or a kubeconfig" || pass "the LLM-facing module holds no fixer token, no GitHub client and no cluster access"
grep -qE "collector|Collector|kubeconfig|--context|EDGELOOP_CONTEXT|[\"']kubectl[\"'], *[\"'](get|apply|delete|patch|create|replace|scale|exec|edit|rollout)" agent/fix_loop.py && fail "fix_loop.py touches the cluster" || pass "fix_loop.py has no cluster access (kubectl is used only for the offline 'kustomize' build)"
grep -rEn '/merge|"merge"|\.merge\(' agent/github.py agent/fix_loop.py agent/fixagent.py >/dev/null 2>&1   && fail "fixer code contains a merge call (L1 means a human merges)" || pass "no merge call in the fixer's code path (L1: a human merges)"
grep -nE "^\s*(import|from)\s+(fastpath|fixagent|fix_loop|github|gitrepo|playbooks|incidents|detector|collector)" policy/gate.py >/dev/null   && fail "policy/gate.py imports from the author side" || pass "the gate imports nothing from the fix agent, the loop or the playbooks (independent verifier)"
python - <<'PY' && pass "every fixable remediation class has an autonomy entry, all capped at L1" || fail "autonomy.yaml does not cover the LLM fix classes"
import sys, yaml
sys.path.insert(0, "agent")
import fixagent
c = yaml.safe_load(open("policy/autonomy.yaml", encoding="utf-8"))["classes"]
assert all(c.get(fixagent.fix_class(r)) == "L1" for r in fixagent.FIXABLE), [fixagent.fix_class(r) for r in fixagent.FIXABLE if c.get(fixagent.fix_class(r)) != "L1"]
PY
grep -q "OPENAI_API_KEY" agent/fixagent.py && ! grep -rn "OPENAI_API_KEY *=\|FIXER_GITHUB_TOKEN *=" --include=*.py --include=*.sh --include=*.yaml . >/dev/null 2>&1 && pass "keys are read from the environment only" || fail "key handling looks wrong"

section "Exit criterion: low false-fix rate on evals, healthy-app never fixed"
if [ $LIVE = 1 ]; then
  [ -n "${OPENAI_API_KEY:-}" ] && [ -n "${FIXER_GITHUB_TOKEN:-}" ] || { fail "--live needs OPENAI_API_KEY and FIXER_GITHUB_TOKEN"; echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="; exit 1; }
  tmp=$(mktemp -d); res="$tmp/results.json"
  export EDGELOOP_LLM_RUN_ID="verify7-$(date +%s)" EDGELOOP_LLM_LEDGER="$tmp/llm_usage.jsonl"
  python evals/run.py run all --fixagent --approve "$APPROVE" $YES --out "$res"; rc=$?
  [ $rc -eq 0 ] && pass "runner exited 0 (every scenario: symptom seen, one PR on the true target and recovery after merge, no PR for the decoy)" || echo "  note  runner exit code $rc (individual scenario failures are judged below)"
  python - "$res" <<'PY' && pass "false-fix rate is 0, healthy-app was never fixed, and no PR touched the wrong app" || fail "exit criterion not met (see above)"
import json, sys
d = json.load(open(sys.argv[1])); rs = d["results"]; a = d["aggregate"]
for r in rs:
    f = r.get("fastpath") or {}
    print(f"    {r['id']:<24} expected={r['expected_action']:<5} prs={f.get('prs')} class={f.get('playbook')} attempts={f.get('attempts')} "
          f"gate_blocked={f.get('gate_blocked')} fixed={r['score']['fixed']} false_fix={r['score']['false_fix']}")
print("    aggregate:", {k: a.get(k) for k in ("fastpath_rate", "fix_rate", "false_fix_rate", "median_time_to_pr_s", "median_time_to_recovery_s")})
bad = []
decoys = [r for r in rs if r["expected_action"] == "none"]
if len(decoys) < 4: bad.append(f"only {len(decoys)} decoy scenario(s) ran; at least 4 are needed")
for r in decoys:
    if (r.get("fastpath") or {}).get("prs"): bad.append(f"decoy {r['id']} got a PR")
if a.get("false_fix_rate") != 0: bad.append(f"false_fix_rate={a.get('false_fix_rate')}")
for r in rs:
    f = r.get("fastpath") or {}
    if r["expected_action"] == "fix" and f.get("prs", 0) > 0 and not f.get("target_ok"): bad.append(f"{r['id']}: PR on the wrong app")
    if f.get("prs", 0) > 1: bad.append(f"{r['id']}: more than one PR for one incident")
assert not bad, bad
PY
else
  skip "live exit-criterion run (rerun with --live; needs OPENAI_API_KEY and FIXER_GITHUB_TOKEN, pushes to origin/main, costs tokens)"
fi
echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
