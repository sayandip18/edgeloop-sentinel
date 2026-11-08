#!/usr/bin/env bash
# Phase 3 verification: one incident per failure, none for the decoy.
#
#   bash scripts/verify-phase3.sh          offline + read-only checks (no Git writes): detector/incident unit tests,
#                                          read-only identity guard, live baseline produces no incident
#   bash scripts/verify-phase3.sh --git    ALSO the real exit-criterion run: every Phase 2 scenario is injected through
#                                          Git (16 commits pushed to origin/main, all reverted, ~30-45 min) with the
#                                          detector running as the diagnoser. Asks, or pass --yes.
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

section "Detector and incident manager (synthetic snapshots)"
out=$(python agent/test_detection.py 2>&1) && pass "$(echo "$out" | tail -1) (agent/test_detection.py)" || fail "detector tests: $out"
out=$(python evals/test_scoring.py 2>&1) && pass "scoring tests incl. detection scoring" || fail "scoring tests: $out"

section "Read-only identity"
bash scripts/make-kubeconfig.sh diagnoser >/dev/null 2>&1 && pass "diagnoser kubeconfig issued" || fail "could not issue diagnoser kubeconfig"
tmp=$(mktemp -d); log="$tmp/inc.jsonl"
kubectl --context "$CTX" config view --raw --minify > "$tmp/admin.kubeconfig" 2>/dev/null
out=$(python agent/watch.py --once --kubeconfig "$tmp/admin.kubeconfig" --log "$log" 2>&1); rc=$?
[ $rc -eq 2 ] && echo "$out" | grep -q "REFUSING" && pass "detector refuses a write-capable kubeconfig" || fail "detector ran with admin credentials (rc=$rc): $out"

section "Live baseline (healthy cluster -> zero incidents)"
for a in oom-app badconfig-app crash-app healthy-app backend-app web-app; do
  r=$(kubectl --context "$CTX" -n apps get deploy "$a" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)
  [ "$r" = "1/1" ] || echo "  note: $a is ${r:-not deployed} (Phase 2 apps must be on main for --git)"
done
out=$(python agent/watch.py --once --log "$log" 2>&1); rc=$?
[ $rc -eq 0 ] && pass "watch --once found no incident on the current cluster" || fail "incident on baseline (rc=$rc): $out $(cat "$log" 2>/dev/null)"

section "Exit criterion: one incident per failure, none for the decoy"
if [ $DO_GIT = 1 ]; then
  res="$tmp/results.json"
  python evals/run.py run all --detector $YES --out "$res" && pass "runner exited 0 (symptoms seen, reverts recovered, detection ok)" || fail "runner reported failure"
  python - "$res" <<'PY' && pass "every scenario: exactly one incident on the true target app; decoy: none" || fail "incident counts wrong (see above)"
import json,sys
d=json.load(open(sys.argv[1])); bad=[]
assert len(d["results"])==8, f"{len(d['results'])} scenarios ran"
for r in d["results"]:
    det=r.get("detection")
    print(f"    {r['id']:<22} incidents={det['count']} expected={det['expected']} app={[i['app'] for i in r['incidents']]}")
    if not det["ok"]: bad.append(r["id"])
assert not bad, bad
PY
else
  skip "end-to-end detection run (rerun with --git; it commits and pushes)"
fi
rm -rf "$tmp"
echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
