#!/usr/bin/env bash
# Stop everything started by scripts/demo-up.sh
set -uo pipefail
cd "$(dirname "$0")/.."
D=agent/state/demo
shopt -s nullglob
n=0
for f in "$D"/*.pid; do
  name=$(basename "$f" .pid); pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    # Git Bash on Windows: also kill the native process if a plain kill did not take
    sleep 1; kill -0 "$pid" 2>/dev/null && { kill -9 "$pid" 2>/dev/null || taskkill //F //PID "$pid" >/dev/null 2>&1; }
    echo "stopped $name (pid $pid)"; n=$((n+1))
  else
    echo "$name was not running"
  fi
  rm -f "$f"
done
[ "$n" = 0 ] && echo "nothing to stop"
exit 0
