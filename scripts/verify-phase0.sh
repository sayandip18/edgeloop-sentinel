#!/usr/bin/env bash
# Phase 0 verification.
#
#   bash scripts/verify-phase0.sh          cluster-only checks (no Git writes):
#                                          RBAC (diagnoser/fixer) + drift revert
#   bash scripts/verify-phase0.sh --git    ALSO runs checks that COMMIT AND PUSH to
#                                          origin/main (asks for confirmation, or pass --yes):
#                                          1. a Git change converges within 2 min
#                                          2. a broken overlay via Git produces OOMKilled
#                                          Each test is reverted with `git revert` + push.
#
# Env: EDGELOOP_CONTEXT (default docker-desktop), CONVERGE_TIMEOUT (default 120s),
#      DRIFT_TIMEOUT (default 150s)
set -uo pipefail
cd "$(dirname "$0")/.."

CTX="${EDGELOOP_CONTEXT:-docker-desktop}"
CONVERGE_TIMEOUT="${CONVERGE_TIMEOUT:-120}"
DRIFT_TIMEOUT="${DRIFT_TIMEOUT:-150}"
DO_GIT=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in --git) DO_GIT=1 ;; --yes) ASSUME_YES=1 ;; *) echo "unknown arg: $a" >&2; exit 2 ;; esac
done

PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
k() { kubectl --context "$CTX" "$@"; }
section() { echo; echo "== $* =="; }

# --- RBAC -------------------------------------------------------------------
# expect_can <kubeconfig> <yes|no> <description> <kubectl auth can-i args...>
expect_can() {
  local kc="$1" want="$2" desc="$3"; shift 3
  local got
  got=$(kubectl --kubeconfig "$kc" auth can-i "$@" 2>/dev/null | head -n1)
  [ -z "$got" ] && got="no"
  if [ "$got" = "$want" ]; then pass "$desc -> $got"; else fail "$desc: expected '$want', got '$got'"; fi
}

check_diagnoser() {
  section "Diagnoser identity (must be read-only)"
  bash scripts/make-kubeconfig.sh diagnoser 10m >/dev/null || { fail "could not issue diagnoser kubeconfig"; return; }
  local kc=kubeconfigs/diagnoser.kubeconfig
  for r in pods pods/log events deployments.apps replicasets.apps configmaps; do
    for v in get list watch; do expect_can "$kc" yes "diagnoser $v $r in apps" "$v" "$r" -n apps; done
  done
  for v in create update patch delete deletecollection; do
    for r in pods deployments.apps configmaps; do expect_can "$kc" no "diagnoser $v $r in apps" "$v" "$r" -n apps; done
  done
  expect_can "$kc" no "diagnoser get secrets in apps"  get secrets -n apps
  expect_can "$kc" no "diagnoser list secrets in apps" list secrets -n apps
  expect_can "$kc" no "diagnoser create pods/exec in apps" create pods/exec -n apps
  expect_can "$kc" no "diagnoser get pods in kube-system" get pods -n kube-system
  expect_can "$kc" no "diagnoser get secrets in kube-system" get secrets -n kube-system
  # Real API calls, not just can-i
  if kubectl --kubeconfig "$kc" -n apps get pods >/dev/null 2>&1; then pass "diagnoser real call: get pods -n apps"; else fail "diagnoser real call: get pods -n apps failed"; fi
  local out
  out=$(kubectl --kubeconfig "$kc" -n apps get secrets 2>&1) && fail "diagnoser real call: get secrets SUCCEEDED" || pass "diagnoser real call: get secrets denied ($(echo "$out" | head -c 90)...)"
  out=$(kubectl --kubeconfig "$kc" -n apps scale deploy/healthy-app --replicas=5 2>&1) && fail "diagnoser real call: scale SUCCEEDED (writes possible!)" || pass "diagnoser real call: scale denied"
}

check_fixer() {
  section "Fixer identity (must be denied everything)"
  bash scripts/make-kubeconfig.sh fixer 10m >/dev/null || { fail "could not issue fixer kubeconfig"; return; }
  local kc=kubeconfigs/fixer.kubeconfig
  expect_can "$kc" no "fixer get pods in apps"        get pods -n apps
  expect_can "$kc" no "fixer list deployments in apps" list deployments.apps -n apps
  expect_can "$kc" no "fixer patch deployments in apps" patch deployments.apps -n apps
  expect_can "$kc" no "fixer create pods in apps"     create pods -n apps
  expect_can "$kc" no "fixer get secrets in apps"     get secrets -n apps
  expect_can "$kc" no "fixer get pods in kube-system" get pods -n kube-system
  # Authoritative: list everything it is allowed to do (api discovery verbs are expected and harmless)
  local listing
  listing=$(kubectl --kubeconfig "$kc" auth can-i --list 2>/dev/null)
  # Allowed: self-review resources, and non-resource URLs with verb [get] only (default discovery).
  if echo "$listing" | awk 'NR>1' | grep -vE '^(selfsubject(access|rules)?reviews)|^\s+\[/[^]]*\]\s+\[\]\s+\[get\]\s*$|^\s*$' | grep -q .; then
    fail "fixer has permissions beyond API discovery:"; echo "$listing" | sed 's/^/        /'
  else
    pass "fixer 'can-i --list' shows only API discovery / self-review"
  fi
}

# --- Drift ------------------------------------------------------------------
check_drift() {
  section "Manual kubectl edit is reverted by Flux (drift correction)"
  local want got
  want=$(k -n apps get deploy healthy-app -o jsonpath='{.spec.replicas}')
  local img_want
  img_want=$(k -n apps get deploy healthy-app -o jsonpath='{.spec.template.spec.containers[0].image}')
  echo "  baseline: replicas=$want image=$img_want"
  k -n apps scale deploy/healthy-app --replicas=$((want+2)) >/dev/null || { fail "could not scale healthy-app (admin context)"; return; }
  k -n apps set image deploy/healthy-app "healthy-app=edgeloop/healthy-app:drifted" >/dev/null 2>&1 || true
  echo "  drifted: replicas=$((want+2)), image=edgeloop/healthy-app:drifted; waiting up to ${DRIFT_TIMEOUT}s for Flux (interval 1m)"
  local t0=$SECONDS ok=0
  while [ $((SECONDS-t0)) -lt "$DRIFT_TIMEOUT" ]; do
    got=$(k -n apps get deploy healthy-app -o jsonpath='{.spec.replicas}/{.spec.template.spec.containers[0].image}')
    if [ "$got" = "$want/$img_want" ]; then ok=1; break; fi
    sleep 5
  done
  if [ $ok = 1 ]; then
    pass "drift reverted by Flux after $((SECONDS-t0))s (replicas+image back to $got)"
  else
    fail "drift NOT reverted after ${DRIFT_TIMEOUT}s (now: $got). Flux Kustomization status:"
    k -n flux-system get kustomization healthy-app -o jsonpath='{.status.conditions}' | sed 's/^/        /'; echo
    echo "        restoring manually..."
    k -n apps scale deploy/healthy-app --replicas="$want" >/dev/null
    k -n apps set image deploy/healthy-app "healthy-app=$img_want" >/dev/null
  fi
}

# --- Git-driven checks ------------------------------------------------------
PUSHED=0   # number of un-reverted test commits
cleanup_git() {
  while [ "$PUSHED" -gt 0 ]; do
    echo "  cleanup: reverting test commit"
    git revert --no-edit HEAD >/dev/null 2>&1 && git push -q origin main && PUSHED=$((PUSHED-1)) || { echo "  cleanup FAILED; revert manually (git log)"; break; }
  done
}

git_precheck() {
  [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "must be on main" >&2; return 1; }
  [ -z "$(git status --porcelain)" ] || { echo "working tree not clean; commit or stash first" >&2; return 1; }
  git fetch -q origin main || return 1
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "local main != origin/main; sync first" >&2; return 1; }
}

commit_push() { # <message>
  git add -A && git commit -q -m "$1" && git push -q origin main && PUSHED=$((PUSHED+1))
}

wait_for() { # <timeout> <description> <cmd...> ; returns 0 if cmd succeeded in time
  local to="$1" desc="$2"; shift 2
  local t0=$SECONDS
  while [ $((SECONDS-t0)) -lt "$to" ]; do
    if "$@" >/dev/null 2>&1; then ELAPSED=$((SECONDS-t0)); return 0; fi
    sleep 3
  done
  ELAPSED=$((SECONDS-t0)); return 1
}
replicas_is() { [ "$(k -n apps get deploy healthy-app -o jsonpath='{.status.readyReplicas}/{.spec.replicas}')" = "$1/$1" ]; }
oom_seen() { [ "$(k -n apps get pods -l app=oom-app -o jsonpath='{range .items[*]}{.status.containerStatuses[0].lastState.terminated.reason}{.status.containerStatuses[0].state.terminated.reason}{"\n"}{end}' | grep -c OOMKilled)" -gt 0 ]; }

check_git_converges() {
  section "A Git commit changes the cluster (healthy-app replicas 1 -> 2)"
  local f=apps/healthy-app/base/deployment.yaml
  grep -q '^  replicas: 1$' "$f" || { fail "$f has no 'replicas: 1' line; cannot run test"; return; }
  sed -i 's/^  replicas: 1$/  replicas: 2/' "$f"
  commit_push "verify-phase0: healthy-app replicas 1 -> 2 (will be reverted)" || { fail "commit/push failed"; return; }
  echo "  pushed $(git rev-parse --short HEAD); waiting up to ${CONVERGE_TIMEOUT}s (no forced reconcile)"
  if wait_for "$CONVERGE_TIMEOUT" "2 replicas ready" replicas_is 2; then
    pass "cluster converged to 2 ready replicas ${ELAPSED}s after push"
  else
    fail "no convergence within ${CONVERGE_TIMEOUT}s"; flux get kustomization healthy-app | sed 's/^/        /'
  fi
  git revert --no-edit HEAD >/dev/null && git push -q origin main && PUSHED=$((PUSHED-1))
  if wait_for 150 "back to 1" replicas_is 1; then pass "revert commit converged back to 1 replica in ${ELAPSED}s"; else fail "did not converge back to 1 replica"; fi
}

check_git_broken() {
  section "A broken overlay deployed via Git fails as designed (oom-app -> OOMKilled)"
  local f=apps/oom-app/overlays/healthy/kustomization.yaml
  cat >> "$f" <<'EOF'
patches:
  - target: {kind: Deployment, name: oom-app}
    patch: |-
      - op: replace
        path: /spec/template/spec/containers/0/env/0/value
        value: "300"
EOF
  commit_push "verify-phase0: apply OOM patch to oom-app (will be reverted)" || { fail "commit/push failed"; return; }
  echo "  pushed $(git rev-parse --short HEAD); waiting up to 240s for OOMKilled"
  if wait_for 240 "OOMKilled" oom_seen; then pass "oom-app OOMKilled ${ELAPSED}s after push"; else fail "no OOMKilled seen in 240s"; k -n apps get pods -l app=oom-app | sed 's/^/        /'; fi
  git revert --no-edit HEAD >/dev/null && git push -q origin main && PUSHED=$((PUSHED-1))
  if wait_for 240 "oom-app ready" k -n apps rollout status deploy/oom-app --timeout=5s; then pass "revert restored oom-app to Ready in ${ELAPSED}s"; else fail "oom-app did not recover after revert"; fi
  local ds
  ds=$(k -n apps get deploy healthy-app -o jsonpath='{.status.readyReplicas}')
  [ "${ds:-0}" -ge 1 ] && pass "healthy-app (decoy) still Ready throughout" || fail "healthy-app not Ready"
}

# --- main -------------------------------------------------------------------
echo "context: $CTX"
section "Flux baseline"
if k -n flux-system get kustomizations -o jsonpath='{range .items[*]}{.metadata.name}={.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | tee /dev/stderr | grep -v '=True' | grep -q .; then
  fail "some Flux kustomizations are not Ready (above)"
else pass "all Flux kustomizations Ready"; fi

check_diagnoser
check_fixer
check_drift

if [ $DO_GIT = 1 ]; then
  if [ $ASSUME_YES = 0 ]; then
    echo; echo "The Git checks will make 4 commits (2 changes + 2 reverts) and PUSH them to origin/main (public)."
    read -r -p "Proceed? [y/N] " ans; [ "$ans" = y ] || [ "$ans" = Y ] || { echo "aborted"; exit 1; }
  fi
  if git_precheck; then
    trap cleanup_git EXIT
    check_git_converges
    check_git_broken
  else
    fail "git precheck failed; Git checks not run"
  fi
else
  section "Git-driven checks"
  skip "convergence + broken-overlay checks (rerun with --git; they commit and push)"
fi

echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
