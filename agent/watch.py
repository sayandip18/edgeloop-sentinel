#!/usr/bin/env python3
"""Detector + incident manager loop (Phase 3). Read-only: uses the diagnoser kubeconfig.

    python agent/watch.py [--kubeconfig kubeconfigs/diagnoser.kubeconfig] [--interval 5] [--log PATH]
                          [--once] [--duration SECONDS]

Prints one line per incident event and appends them to the JSONL log (default agent/state/incidents.jsonl).
`--once` does a single pass and exits 0 if no incident is open, 1 otherwise.
Exit codes: 0 ok, 2 refused to start (identity can write / bad credentials), 3 cluster unreachable too long.
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collector import Collector, CollectorError  # noqa: E402
from detector import Config, detect  # noqa: E402
from incidents import IncidentManager  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAX_CONSECUTIVE_ERRORS = 12


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kubeconfig", default=str(ROOT / "kubeconfigs" / "diagnoser.kubeconfig"))
    p.add_argument("--namespace", default="apps")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--log", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
    p.add_argument("--resolve-after", type=float, default=45.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = run until interrupted)")
    a = p.parse_args()

    if not Path(a.kubeconfig).is_file():
        print(f"kubeconfig not found: {a.kubeconfig}\nrun: bash scripts/make-kubeconfig.sh diagnoser", file=sys.stderr); return 2
    col = Collector(a.kubeconfig, a.namespace)
    try:
        col.assert_read_only()
    except CollectorError as e:
        print(str(e), file=sys.stderr); return 2

    mgr = IncidentManager(a.log, a.resolve_after)
    cfg, errors, started, t0 = Config(), 0, False, time.time()
    while True:
        try:
            snap = col.snapshot()
            errors = 0
        except CollectorError as e:
            if e.fatal:
                print(f"fatal: {e}", file=sys.stderr); return 2
            errors += 1
            print(f"warn: {e} ({errors}/{MAX_CONSECUTIVE_ERRORS})", file=sys.stderr, flush=True)
            if errors >= MAX_CONSECUTIVE_ERRORS:
                return 3
            time.sleep(a.interval); continue
        for ev in mgr.observe(detect(snap, cfg=cfg)):
            inc = ev["incident"]
            print(f"{ev['event']:<9} {inc['id']} app={inc['app']} affected={inc['affected_apps']} "
                  f"signals={[s['type'] for s in inc['signals']]}", flush=True)
        if not started:
            # tells the eval runner the baseline pass is done, so anything after this is attributable to the injection
            Path(a.log).parent.mkdir(parents=True, exist_ok=True)
            with Path(a.log).open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "event": "watch_started"}) + "\n")
            started = True
        if a.once:
            return 1 if mgr.open else 0
        if a.duration and time.time() - t0 >= a.duration:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
