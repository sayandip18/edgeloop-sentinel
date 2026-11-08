"""Runs for CRASH_AFTER_SECONDS then exits 1. 0 means run forever."""
import os, sys, time
after = int(os.environ.get("CRASH_AFTER_SECONDS", "0"))
print(f"started, crash_after={after}", flush=True)
start = time.time()
while True:
    time.sleep(1)
    if after and time.time() - start >= after:
        print("fatal: unrecoverable internal error, exiting", flush=True)
        sys.exit(1)
