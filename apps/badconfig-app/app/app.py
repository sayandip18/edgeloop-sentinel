"""Requires LISTEN_PORT (integer 1-65535) from its ConfigMap, else exits 2."""
import os, sys, time
raw = os.environ.get("LISTEN_PORT")
if raw is None:
    print("fatal: required config key LISTEN_PORT is missing", flush=True); sys.exit(2)
try:
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("out of range")
except ValueError:
    print(f"fatal: LISTEN_PORT={raw!r} is not a valid port", flush=True); sys.exit(2)
print(f"config ok, would listen on {port}", flush=True)
while True:
    time.sleep(30)
