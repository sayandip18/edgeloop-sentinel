#!/usr/bin/env bash
# Phase 4 verification: known failures self-heal via PR (deterministic playbooks, no LLM, L1 human approval).
#
#   bash scripts/verify-phase4.sh          offline checks: playbook/fast-path tests against the real apps/ tree, static
#                                          safety checks, credential + branch-protection preflight (SKIPped if no token)
#   bash scripts/verify-phase4.sh --git    ALSO the real exit-criterion run: all 8 scenarios injected through Git, the fast
#                                          path opens PRs on GitHub, this script's runner merges them (--approve auto), the
#                                          cluster must recover. Needs FIXER_GITHUB_TOKEN, protected main, a diagnoser
#                                          kubeconfig. Pushes ~24 commits to origin/main and opens 7 PRs. Asks, or pass --yes.
#                                          Pass --manual to merge the PRs yourself on GitHub instead.
set -uo pipefail
cd "$(dirname "$0")/.."
DO_GIT=0; YES=""; APPROVE=auto
for a in "$@"; do case "$a" in --git) DO_GIT=1 ;; --yes) YES=--yes ;; --manual) APPROVE=manual ;; *) echo "unknown arg: $a" >&2; exit 2 ;; esac; done
PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
section() { echo; echo "== $* =="; }

section "Playbooks and fast path (real apps/ tree, throwaway git repo, fake GitHub)"
out=$(python agent/test_fastpath.py 2>&1) && pass "$(echo "$out" | grep -c '^ok') fast-path tests (agent/test_fastpath.py)" || fail "fast-path tests: $out"
out=$(python evals/test_scoring.py 2>&1) && pass "scoring tests incl. fast-path scoring" || fail "scoring tests: $out"
out=$(python agent/test_detection.py 2>&1) && pass "detector tests still pass" || fail "detector tests: $out"
out=$(python evals/run.py validate 2>&1) && pass "$out" || fail "scenario validation: $out"
out=$(python evals/run.py run all --fastpath --dry-run 2>&1) && pass "runner --fastpath dry run" || fail "runner dry run: $out"
for o in apps/*/overlays/*/; do kubectl kustomize "$o" >/dev/null 2>&1 || fail "kustomize $o does not build"; done
pass "every overlay still builds"

section "Static safety checks (principles 2 and 4)"
grep -rEn '/merge|"merge"|\.merge\(' agent/github.py agent/fastpath.py playbooks/ >/dev/null 2>&1 \
  && fail "fast path code contains a merge call (L1 means a human merges)" || pass "no merge call in the fixer's code path"
uses=$(grep -c 'subprocess.run(\[kubectl' agent/fastpath.py); ok=$(grep -c 'subprocess.run(\[kubectl, "kustomize"' agent/fastpath.py)
[ "$uses" = "$ok" ] && [ "$uses" -ge 1 ] && ! grep -q kubectl playbooks/*.py agent/github.py agent/gitrepo.py   && pass "fast path only runs 'kubectl kustomize' (offline); no cluster access" || fail "fast path uses kubectl for something other than 'kustomize'"
grep -rn "kubeconfig\|KUBECONFIG" agent/fastpath.py agent/github.py agent/gitrepo.py playbooks/ | grep -v "^.*:.*#" >/dev/null 2>&1 \
  && fail "fast path code references a kubeconfig" || pass "fast path never loads a kubeconfig"
out=$(env -u FIXER_GITHUB_TOKEN python agent/fastpath.py run --once --incidents /dev/null 2>&1); rc=$?
[ $rc -eq 2 ] && pass "fast path refuses to start without a token" || fail "started without a token (rc=$rc): $out"
tmp=$(mktemp -d); : > "$tmp/inc.jsonl"
out=$(python agent/fastpath.py run --dry-run --once --incidents "$tmp/inc.jsonl" --state "$tmp/fp.jsonl" 2>&1); rc=$?
[ $rc -eq 0 ] && pass "dry run with no incidents exits 0 and does nothing" || fail "dry run failed (rc=$rc): $out"
rm -rf "$tmp"

section "Credential and branch protection"
REPO=$(python - <<'PY'
import sys; sys.path.insert(0, "agent")
from fastpath import repo_from_origin, ROOT
print(repo_from_origin(ROOT) or "")
PY
)
if [ -z "$REPO" ]; then fail "cannot work out owner/name from origin"; else
  prot=$(python - "$REPO" <<'PY'
import json, sys, urllib.request
try:
    r = urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{sys.argv[1]}/branches/main", headers={"User-Agent": "verify"}), timeout=20)
    print("yes" if json.load(r).get("protected") else "no")
except Exception as e:
    print("unknown")
PY
)
  case "$prot" in
    yes) pass "main on $REPO is protected" ;;
    no) fail "main on $REPO is NOT protected: Settings > Branches > add a rule for main that requires a pull request (docs/phase4.md)" ;;
    *) skip "could not read branch protection anonymously (private repo or offline)" ;;
  esac
fi
if [ -n "${FIXER_GITHUB_TOKEN:-}" ]; then
  out=$(python - <<'PY' 2>&1
import os, sys; sys.path.insert(0, "agent")
from fastpath import repo_from_origin, ROOT
from github import GitHub, preflight
gh = GitHub(repo_from_origin(ROOT), os.environ["FIXER_GITHUB_TOKEN"])
p = preflight(gh)
print("\n".join(p)); sys.exit(1 if p else 0)
PY
) && pass "fixer token valid, has push access, is not admin, main is protected" || fail "token preflight: $out"
else
  skip "FIXER_GITHUB_TOKEN not set: token preflight skipped (docs/phase4.md explains how to create it)"
fi

section "Exit criterion: known failures self-heal via PR"
if [ $DO_GIT = 1 ]; then
  [ -n "${FIXER_GITHUB_TOKEN:-}" ] || { fail "--git needs FIXER_GITHUB_TOKEN"; echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="; exit 1; }
  tmp=$(mktemp -d); res="$tmp/results.json"
  python evals/run.py run all --fastpath --approve "$APPROVE" $YES --out "$res" && pass "runner exited 0 (every fix recovered via its PR, decoy untouched, detection ok)" || fail "runner reported failure"
  python - "$res" <<'PY' && pass "every fix scenario: exactly one PR on the true target app, cluster recovered; decoy: no PR" || fail "PR outcome wrong (see above)"
import json, sys
d = json.load(open(sys.argv[1])); bad = []
assert len(d["results"]) == 8, f"{len(d['results'])} scenarios ran"
for r in d["results"]:
    f, s = r["fastpath"], r["score"]
    print(f"    {r['id']:<22} prs={f['prs']} expected={f['expected']} playbook={f['playbook']} to_pr_s={f['time_to_pr_s']} fixed={s['fixed']} false_fix={s['false_fix']}")
    if not f["ok"] or (r["expected_action"] == "fix" and not s["fixed"]) or s["false_fix"]: bad.append(r["id"])
print("    aggregate:", {k: d["aggregate"][k] for k in ("fastpath_rate", "fix_rate", "false_fix_rate", "median_time_to_pr_s", "median_time_to_recovery_s")})
assert not bad, bad
PY
  rm -rf "$tmp"
else
  skip "end-to-end run (rerun with --git; it commits, pushes, opens PRs and merges them)"
fi
echo; echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[ $FAIL -eq 0 ]
