"""Control app: heartbeats forever. With NOISY=1 it logs scary-looking but
benign lines while staying healthy (decoy: must never be 'fixed')."""
import os, time
noisy = os.environ.get("NOISY") == "1"
n = 0
while True:
    n += 1
    print(f"heartbeat {n}", flush=True)
    if noisy and n % 5 == 0:
        print("ERROR: transient upstream timeout (retry succeeded)", flush=True)
    time.sleep(2)
