#!/usr/bin/env bash
# Start the sentinel agent processes in the background for a local demo.
# Usage: bash scripts/demo-up.sh [--merger] [--no-llm]
#   (default)  detector + fast path                      (deterministic PRs, you merge them)
#   --no-llm   explicitly skip the LLM diagnosis/fix loops (same as default; kept for clarity)
#   --llm      also start diagnose_loop + fix_loop        (needs OPENAI_API_KEY)
#   --merger   also start the merger                      (needs EDGELOOP_MERGER_TOKEN, EDGELOOP_FIXER_LOGIN)
# Logs: agent/state/demo/<name>.log   PIDs: agent/state/demo/<name>.pid   Stop: bash scripts/demo-down.sh
set -euo pipefail
cd "$(dirname "$0")/.."

LLM=0; MERGER=0
for a in "$@"; do
  case "$a" in
    --llm) LLM=1 ;;
    --no-llm) LLM=0 ;;
    --merger) MERGER=1 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

D=agent/state/demo
mkdir -p "$D"

if [ ! -f .env ]; then echo "ERROR: .env not found" >&2; exit 1; fi
if [ ! -f kubeconfigs/diagnoser.kubeconfig ]; then
  echo "ERROR: kubeconfigs/diagnoser.kubeconfig missing. Run: bash scripts/make-kubeconfig.sh diagnoser 8h" >&2; exit 1
fi

# Python executable (Windows Git Bash may only have `python`).
PY=python; command -v python >/dev/null 2>&1 || PY=python3

# start <name> <with_env 0|1> <command...>
start() {
  local name="$1" with_env="$2"; shift 2
  if [ -f "$D/$name.pid" ] && kill -0 "$(cat "$D/$name.pid")" 2>/dev/null; then
    echo "  $name already running (pid $(cat "$D/$name.pid"))"; return
  fi
  if [ "$with_env" = 1 ]; then
    # tokens only for processes that need them; strip CRs in case .env has Windows line endings
    ( set -a; source <(tr -d '\r' < .env); set +a
      PYTHONUNBUFFERED=1 nohup "$@" >"$D/$name.log" 2>&1 & echo $! >"$D/$name.pid" )
  else
    # detector: read-only identity, must not see any token
    ( PYTHONUNBUFFERED=1 nohup env -u FIXER_GITHUB_TOKEN -u EDGELOOP_MERGER_TOKEN -u OPENAI_API_KEY "$@" \
        >"$D/$name.log" 2>&1 & echo $! >"$D/$name.pid" )
  fi
  sleep 2
  if kill -0 "$(cat "$D/$name.pid")" 2>/dev/null; then
    echo "  started $name (pid $(cat "$D/$name.pid"))"
  else
    echo "  FAILED  $name exited immediately; last log lines:"; tail -n 5 "$D/$name.log" | sed 's/^/      /'
    rm -f "$D/$name.pid"
  fi
}

echo "Starting sentinel demo processes..."
start watch    0 $PY agent/watch.py
start fastpath 1 $PY agent/fastpath.py run
if [ "$LLM" = 1 ]; then
  start diagnose 1 $PY agent/diagnose_loop.py
  start fixloop  1 $PY agent/fix_loop.py run --fastpath-state agent/state/fastpath.jsonl
fi
if [ "$MERGER" = 1 ]; then
  start merger   1 $PY agent/merger.py run
fi

echo
echo "Logs:  tail -f $D/*.log"
echo "Stop:  bash scripts/demo-down.sh"
