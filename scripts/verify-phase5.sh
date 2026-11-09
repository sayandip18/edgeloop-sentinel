#!/usr/bin/env bash
# Phase 5 verification: deliberately bad patches are blocked by the gate or reverted by the watchdog.
#
#   bash scripts/verify-phase5.sh             offline: gate bad-patch matrix, risk engine, watchdog revert tests (local bare
#                                             repo), fast-path wiring, static safety checks. No cluster, no pushes.
#   bash scripts/verify-phase5.sh --cluster   ALSO live: reproduce-then-fix in a real vcluster on the docker-desktop context
#                                             (oom-app broken -> healthy must recover; a still-broken patch must NOT).
#                                             Creates and deletes a vcluster and a namespace; needs vcluster + a running cluster.
set -uo pipefail
cd "$(dirname "$0")/.."
LIVE=0
for a in "$@"; do case "$a" in --cluster) LIVE=1 ;; *) echo "unknown arg: $a" >&2; exit 2 ;; esac; done
PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
section() { echo; echo "== $* =="; }

section "Tools the gate needs"
for t in kubeconform kube-linter vcluster; do
  p=$(python -c "import sys; sys.path.insert(0,'policy'); import gate; print(gate.find_tool('$t') or '')")
  [ -n "$p" ] && pass "$t found" || { [ $t = vcluster ] && skip "vcluster not installed (only --cluster needs it)" || fail "$t missing (winget install; the gate reports 'incomplete' without it)"; }
done

section "Validation gate: bad patches are blocked (real apps/ tree, throwaway repo)"
out=$(python policy/test_gate.py 2>&1) && pass "$(echo "$out" | grep -c '^ok') gate and risk tests: RBAC, NetworkPolicy, PVC, privileged, :latest, hostPath, oversize, out-of-scope, schema, lint, ... blocked; good patches pass" || fail "gate tests: $out"

section "Rollback watchdog: unhealthy or silent changes are reverted (local bare repo, fake health)"
out=$(python watchdog/test_watchdog.py 2>&1) && pass "$(echo "$out" | grep -c '^ok') watchdog tests (revert, confirm, dead-man's switch, ignores non-sentinel commits, dry-run, kill switch, no forced revert)" || fail "watchdog tests: $out"

section "Fast path with the gate in its loop"
out=$(python agent/test_fastpath.py 2>&1) && pass "$(echo "$out" | grep -c '^ok') fast-path tests (a blocked or incomplete gate opens no PR)" || fail "fast-path tests: $out"
out=$(python agent/test_detection.py 2>&1) && pass "detector tests still pass" || fail "detector tests: $out"
out=$(python evals/test_scoring.py 2>&1) && pass "scoring tests still pass" || fail "scoring tests: $out"

section "Static safety checks (principles 2 and 5)"
grep -nE "^\s*(import|from)\s+(fastpath|github|gitrepo|playbooks|incidents|detector|collector)\b" policy/gate.py policy/sandbox.py >/dev/null \
  && fail "the gate imports author-side code (it must be independent of the fixer)" || pass "gate and sandbox import nothing from agent/ or playbooks/"
grep -nE "\"(push|merge|commit)\"|'(push|merge|commit)'" policy/gate.py policy/sandbox.py policy/risk.py >/dev/null \
  && fail "gate/sandbox/risk contain a git write" || pass "gate, sandbox and risk never write to Git"
bad=$(grep -nE "NS, \"(apply|delete|create|patch|replace|scale|rollout|exec|edit|label|annotate)\"" watchdog/watchdog.py | grep -v "^.*#" || true)
[ -z "$bad" ] && pass "watchdog's cluster access is read-only (kubectl get only)" || fail "watchdog uses a mutating kubectl verb: $bad"
grep -q "FIXER_GITHUB_TOKEN" watchdog/watchdog.py && fail "watchdog references the fixer token (it must use the host's own git identity)" || pass "watchdog does not use the fixer's PAT"
grep -q "kubeconfig" agent/fastpath.py && fail "fast path references a kubeconfig" || pass "fast path still has no cluster access"
out=$(python policy/gate.py check --base-ref HEAD --changes-json - --json <<<'{"infra/rbac/diagnoser.yaml":"x: 1\n"}' 2>&1); rc=$?
[ $rc -eq 1 ] && pass "gate CLI exits 1 on an out-of-scope patch" || fail "gate CLI exit code $rc: $out"
for o in apps/*/overlays/*/; do kubectl kustomize "$o" >/dev/null 2>&1 || fail "kustomize $o does not build"; done
pass "every overlay still builds"

section "Sandbox: reproduce-then-fix in a vcluster"
if [ $LIVE = 1 ]; then
  python - <<'PY' && pass "oom-app broken -> reproduced; healthy -> recovered; a still-broken patch does not recover" || fail "sandbox run (see above)"
import subprocess, sys
sys.path.insert(0, "policy")
import sandbox
def build(o): return subprocess.run(["kubectl", "kustomize", f"apps/oom-app/overlays/{o}"], capture_output=True, text=True, check=True).stdout
broken, healthy = build("broken"), build("healthy")
r1 = sandbox.reproduce_then_fix(broken, healthy, app="oom-app")
print("   ", r1)
assert r1["reproduced"] and r1["recovered"], "good fix must reproduce the failure and recover"
r2 = sandbox.reproduce_then_fix(broken, broken, app="oom-app")
print("   ", r2)
assert not r2["recovered"], "a patch that leaves the app broken must NOT pass"
PY
else
  skip "live sandbox run (rerun with --cluster; needs vcluster and a running docker-desktop cluster)"
fi
echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
