"""Allocates ALLOC_MB of resident memory in 8 MiB steps, then idles."""
import os, time
target = int(os.environ.get("ALLOC_MB", "32"))
held = []
for i in range(0, target, 8):
    held.append(b"x" * (min(8, target - i) * 1024 * 1024))  # touches pages
    print(f"allocated {min(i + 8, target)}/{target} MiB", flush=True)
    time.sleep(0.2)
print("allocation complete, idling", flush=True)
while True:
    time.sleep(30)
