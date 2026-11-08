# agent

Phase 3 (built): rule-based detector and incident manager. Read-only. See [docs/phase3.md](../docs/phase3.md).

| File | Role |
| --- | --- |
| `detector.py` | Pure rules: raw Kubernetes JSON in, signals out. No I/O, no log text |
| `incidents.py` | Dedupes and correlates signals into one incident per failure; JSONL log |
| `collector.py` | The only code that talks to the cluster; diagnoser kubeconfig, refuses write-capable identities |
| `watch.py` | The loop: `python agent/watch.py [--once]` |
| `test_detection.py` | Offline tests with synthetic snapshots for every Phase 2 scenario |

Context builder, diagnosis and fix agents arrive in Phases 6-7.
