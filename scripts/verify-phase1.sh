#!/usr/bin/env bash
# Phase 1 verification: every injected failure is visible in metrics and logs.
#
#   bash scripts/verify-phase1.sh          stack checks only (no Git writes):
#                                          Flux/Helm health, scrape targets, logs flowing,
#                                          identity boundaries (obs-deployer, diagnoser)
#   bash scripts/verify-phase1.sh --git    ALSO breaks oom/badconfig/crash apps through Git (one commit),
#                                          checks metrics + Loki for each, and that healthy-app shows nothing wrong.
#                                          Commits and PUSHES to origin/main (asks, or pass --yes), then reverts.
#
# Prometheus/Loki are queried through the API server's service proxy using the admin kubeconfig context.
# The diagnoser deliberately has NO route to them yet (see docs/phase1.md); this script asserts that.
#
# Env: EDGELOOP_CONTEXT (default docker-desktop), SIGNAL_TIMEOUT (default 360s)
set -uo pipefail
cd "$(dirname "$0")/.."

CTX="${EDGELOOP_CONTEXT:-docker-desktop}"
SIGNAL_TIMEOUT="${SIGNAL_TIMEOUT:-360}"
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

PROM=/api/v1/namespaces/observability/services/kps-prometheus:http-web/proxy
LOKI=/api/v1/namespaces/observability/services/loki:http-metrics/proxy

urlenc() { python -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$1"; }

# prom_count <promql> -> number of series returned (0 on any error)
prom_count() {
  k get --raw "$PROM/api/v1/query?query=$(urlenc "$1")" 2>/dev/null |
    python -c 'import sys,json
try: print(len(json.load(sys.stdin)["data"]["result"]))
except Exception: print(0)'
}
# prom_value <promql> -> first series value ("" if none)
prom_value() {
  k get --raw "$PROM/api/v1/query?query=$(urlenc "$1")" 2>/dev/null |
    python -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["result"][0]["value"][1])
except Exception: print("")'
}
# loki_count <logql> -> number of log lines matching in the last 15m
loki_count() {
  k get --raw "$LOKI/loki/api/v1/query_range?limit=1000&since=15m&query=$(urlenc "$1")" 2>/dev/null |
    python -c 'import sys,json
try: print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))
except Exception: print(0)'
}

ELAPSED=0
wait_for() { # <timeout> <cmd...>
  local to="$1"; shift
  local t0=$SECONDS
  while [ $((SECONDS-t0)) -lt "$to" ]; do
    if "$@" >/dev/null 2>&1; then ELAPSED=$((SECONDS-t0)); return 0; fi
    sleep 5
  done
  ELAPSED=$((SECONDS-t0)); return 1
}
gt0() { [ "$(prom_count "$1")" -gt 0 ]; }
loki_gt0() { [ "$(loki_count "$1")" -gt 0 ]; }

# --- stack health -----------------------------------------------------------
check_stack() {
  section "Observability stack is deployed by Flux and healthy"
  local n
  for n in observability-rbac observability observability-rules; do
    [ "$(k -n flux-system get kustomization "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ] \
      && pass "Flux Kustomization $n Ready" || fail "Flux Kustomization $n not Ready"
  done
  for n in kube-prometheus-stack loki alloy; do
    [ "$(k -n flux-system get helmrelease "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ] \
      && pass "HelmRelease $n Ready" || fail "HelmRelease $n not Ready"
  done
  local bad
  bad=$(k -n observability get pods --no-headers 2>/dev/null | awk '{split($2,a,"/"); if ($3!="Running" || a[1]!=a[2]) print $1" "$3" "$2}')
  [ -z "$bad" ] && pass "all pods in observability Running and ready" || fail "pods not ready: $bad"
  [ "$(k -n observability get pods --no-headers 2>/dev/null | wc -l)" -gt 0 ] || fail "no pods in observability"
}

check_pipeline() {
  section "Metrics and logs are flowing"
  [ "$(prom_count 'up{job="kube-state-metrics"} == 1')" -ge 1 ] && pass "Prometheus scrapes kube-state-metrics" || fail "kube-state-metrics target not up"
  [ "$(prom_count 'up{job="kubelet"} == 1')" -ge 1 ] && pass "Prometheus scrapes kubelet" || fail "kubelet target not up"
  [ "$(prom_count 'kube_pod_status_ready{namespace="apps"}')" -ge 1 ] && pass "pod state for namespace apps is in Prometheus" || fail "no kube_pod_status_ready series for apps"
  local a
  for a in oom-app badconfig-app crash-app healthy-app; do
    [ "$(loki_count "{namespace=\"apps\",app=\"$a\"}")" -gt 0 ] && pass "Loki has logs for $a" || fail "Loki has no logs for $a (last 15m)"
  done
  local ns
  ns=$(loki_count '{namespace!="apps"}')
  [ "$ns" -eq 0 ] && pass "only the apps namespace is collected" || fail "Loki holds logs from other namespaces ($ns lines)"
}

# --- identity boundaries ----------------------------------------------------
can() { # <as> <yes|no> <desc> <can-i args...>
  local as="$1" want="$2" desc="$3"; shift 3
  local got; got=$(k auth can-i --as="$as" "$@" 2>/dev/null | head -n1); [ -z "$got" ] && got=no
  [ "$got" = "$want" ] && pass "$desc -> $got" || fail "$desc: expected '$want', got '$got'"
}
check_identities() {
  section "Identity boundaries"
  local od=system:serviceaccount:flux-system:obs-deployer
  local dg=system:serviceaccount:observability:diagnoser
  local dp=system:serviceaccount:flux-system:deployer
  can "$od" yes "obs-deployer create deployments in observability" create deployments.apps -n observability
  can "$od" yes "obs-deployer create CRDs" create customresourcedefinitions.apiextensions.k8s.io
  can "$od" no  "obs-deployer create deployments in apps" create deployments.apps -n apps
  can "$od" no  "obs-deployer get secrets in apps" get secrets -n apps
  can "$od" no  "obs-deployer get secrets in kube-system" get secrets -n kube-system
  can "$od" no  "obs-deployer create pods/exec in observability" create pods/exec -n observability
  can "$dp" no  "deployer (apps) create CRDs" create customresourcedefinitions.apiextensions.k8s.io
  can "$dp" no  "deployer (apps) create deployments in observability" create deployments.apps -n observability
  can "$dg" no  "diagnoser create deployments in observability" create deployments.apps -n observability
  can "$dg" no  "diagnoser get secrets in observability" get secrets -n observability
  can "$dg" no  "diagnoser proxy services in observability without a name (only the two named query services, since Phase 6)" get services/proxy -n observability
  can "$dg" no  "diagnoser get pods/log in observability" get pods/log -n observability
}

# --- Git-driven failure injection ------------------------------------------
PUSHED=0
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

check_failures() {
  section "Injected failures are visible (breaking oom, badconfig, crash apps via Git in one commit)"
  local a
  for a in oom-app badconfig-app crash-app; do
    cp "apps/$a/overlays/broken/kustomization.yaml" "apps/$a/overlays/healthy/kustomization.yaml"
  done
  git add -A && git commit -q -m "verify-phase1: apply broken overlays to oom/badconfig/crash apps (will be reverted)" \
    && git push -q origin main && PUSHED=$((PUSHED+1)) || { fail "commit/push failed"; return; }
  echo "  pushed $(git rev-parse --short HEAD); waiting up to ${SIGNAL_TIMEOUT}s per signal"

  # oom-app
  wait_for "$SIGNAL_TIMEOUT" gt0 'kube_pod_container_status_last_terminated_reason{namespace="apps",pod=~"oom-app.*",reason="OOMKilled"} == 1' \
    && pass "oom-app: OOMKilled in metrics (${ELAPSED}s)" || fail "oom-app: no OOMKilled in metrics"
  [ "$(prom_value 'max(kube_pod_container_status_last_terminated_exitcode{namespace="apps",pod=~"oom-app.*"})')" = 137 ] \
    && pass "oom-app: exit code 137 in metrics" || fail "oom-app: exit code 137 not in metrics"
  wait_for "$SIGNAL_TIMEOUT" gt0 'increase(kube_pod_container_status_restarts_total{namespace="apps",pod=~"oom-app.*"}[10m]) > 0' \
    && pass "oom-app: restarts in metrics" || fail "oom-app: no restarts in metrics"
  wait_for 60 loki_gt0 '{app="oom-app"} |= "allocated"' \
    && pass "oom-app: allocation log lines in Loki" || fail "oom-app: no log lines in Loki"

  # badconfig-app
  wait_for "$SIGNAL_TIMEOUT" gt0 'kube_pod_container_status_last_terminated_exitcode{namespace="apps",pod=~"badconfig-app.*"} == 2' \
    && pass "badconfig-app: exit code 2 in metrics (${ELAPSED}s)" || fail "badconfig-app: exit code 2 not in metrics"
  wait_for 60 loki_gt0 '{app="badconfig-app"} |= "is not a valid port"' \
    && pass "badconfig-app: config error in Loki" || fail "badconfig-app: config error not in Loki"

  # crash-app
  wait_for "$SIGNAL_TIMEOUT" gt0 'kube_pod_container_status_waiting_reason{namespace="apps",pod=~"crash-app.*",reason="CrashLoopBackOff"} == 1 or increase(kube_pod_container_status_restarts_total{namespace="apps",pod=~"crash-app.*"}[10m]) > 1' \
    && pass "crash-app: restarts / CrashLoopBackOff in metrics (${ELAPSED}s)" || fail "crash-app: no restarts in metrics"
  wait_for 60 loki_gt0 '{app="crash-app"} |= "fatal: unrecoverable"' \
    && pass "crash-app: fatal log line in Loki" || fail "crash-app: fatal line not in Loki"

  # alert rules reflect it
  wait_for 180 gt0 'ALERTS{alertstate="firing",alertname=~"AppContainerOOMKilled|AppContainerRestarting|AppCrashLoopBackOff"}' \
    && pass "demo alert rules are firing for the broken apps" || fail "no demo alert firing"

  # decoy
  [ "$(prom_count 'kube_pod_container_status_restarts_total{namespace="apps",pod=~"healthy-app.*"} > 0')" -eq 0 ] \
    && pass "healthy-app: zero restarts" || fail "healthy-app: has restarts"
  [ "$(prom_count 'kube_pod_status_ready{namespace="apps",pod=~"healthy-app.*",condition="true"} == 1')" -ge 1 ] \
    && pass "healthy-app: Ready" || fail "healthy-app: not Ready"
  [ "$(prom_count 'ALERTS{alertstate="firing",pod=~"healthy-app.*"}')" -eq 0 ] \
    && pass "healthy-app: no alerts firing" || fail "healthy-app: alert firing"
  [ "$(loki_count '{app="healthy-app"} |~ "(?i)fatal|error|traceback"')" -eq 0 ] \
    && pass "healthy-app: no error lines in Loki" || fail "healthy-app: error lines in Loki"

  git revert --no-edit HEAD >/dev/null && git push -q origin main && PUSHED=$((PUSHED-1))
  recovered() { [ "$(k -n apps get pods --no-headers | awk '$3!="Running"' | wc -l)" -eq 0 ]; }
  wait_for 300 recovered && pass "revert restored all apps to Running (${ELAPSED}s)" || fail "apps did not recover after revert"
}

# --- main -------------------------------------------------------------------
echo "context: $CTX"
check_stack
check_pipeline
check_identities

if [ $DO_GIT = 1 ]; then
  if [ $ASSUME_YES = 0 ]; then
    echo; echo "The Git checks make 2 commits (inject + revert) and PUSH them to origin/main (public)."
    read -r -p "Proceed? [y/N] " ans; [ "$ans" = y ] || [ "$ans" = Y ] || { echo "aborted"; exit 1; }
  fi
  if git_precheck; then trap cleanup_git EXIT; check_failures; else fail "git precheck failed; Git checks not run"; fi
else
  section "Failure visibility"
  skip "break-each-app checks (rerun with --git; they commit and push)"
fi

echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
