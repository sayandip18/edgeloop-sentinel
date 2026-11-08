#!/usr/bin/env bash
# Phase 2 verification: one command injects a failure and scores it.
#
#   bash scripts/verify-phase2.sh          offline checks (no Git writes): scenario schema + coverage, every overlay
#                                          builds, scoring unit tests, runner dry run, new apps healthy in the cluster
#   bash scripts/verify-phase2.sh --git    ALSO runs the real exit-criterion check: `eval.sh run oom-kill` and the
#                                          decoy (4 commits pushed to origin/main, all reverted). Asks, or pass --yes.
set -uo pipefail
cd "$(dirname "$0")/.."
CTX="${EDGELOOP_CONTEXT:-docker-desktop}"
DO_GIT=0; YES=""
for a in "$@"; do case "$a" in --git) DO_GIT=1 ;; --yes) YES=--yes ;; *) echo "unknown arg: $a" >&2; exit 2 ;; esac; done
PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
section() { echo; echo "== $* =="; }

section "Scenario definitions"
out=$(python evals/run.py validate 2>&1) && pass "$out" || fail "scenario validation: $out"
for id in oom-kill bad-config app-crash bad-image-tag wrong-env-var failing-probe dependency-outage decoy-noisy-healthy; do
  [ -f "evals/scenarios/$id.yaml" ] && pass "scenario $id exists" || fail "scenario $id missing"
done

section "Every overlay builds"
for o in apps/*/overlays/*/; do
  kubectl kustomize "$o" >/dev/null 2>&1 && pass "kustomize $o" || fail "kustomize $o does not build"
done

section "Scoring logic"
out=$(python evals/test_scoring.py 2>&1) && pass "scoring unit tests ($(echo "$out" | wc -l) groups)" || fail "scoring tests: $out"

section "Runner dry run"
out=$(python evals/run.py run all --dry-run 2>&1) && echo "$out" | grep -q "no Git or cluster changes made" \
  && pass "dry run plans all scenarios and changes nothing" || fail "dry run failed: $out"
[ -z "$(git status --porcelain apps)" ] || echo "  note: apps/ has uncommitted changes (expected before the Phase 2 commit)"

section "Cluster baseline (needed for injection)"
for a in oom-app badconfig-app crash-app healthy-app backend-app web-app; do
  r=$(kubectl --context "$CTX" -n apps get deploy "$a" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)
  [ "$r" = "1/1" ] && pass "$a Ready ($r)" || fail "$a not Ready (${r:-not deployed}); push Phase 2 files and apply infra/flux/kustomizations.yaml"
done

section "One command injects and scores a failure"
if [ $DO_GIT = 1 ]; then
  res=$(mktemp)
  python evals/run.py run oom-kill decoy-noisy-healthy $YES --out "$res" && pass "runner exited 0 (symptoms seen, reverts recovered)" || fail "runner reported failure"
  python - "$res" <<'PY' && pass "result file has scored records for oom-kill and decoy" || fail "result file missing or incomplete"
import json,sys
d=json.load(open(sys.argv[1])); ids={r["id"] for r in d["results"]}
assert ids=={"oom-kill","decoy-noisy-healthy"}, ids
assert all(r["score"]["harness_ok"] for r in d["results"])
assert d["aggregate"]["fix_rate"] is None   # no responder: fix metrics undefined, not zero
PY
else
  skip "end-to-end injection (rerun with --git; it commits and pushes)"
fi

echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
