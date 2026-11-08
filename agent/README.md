# agent

Phase 3 (built): rule-based detector and incident manager. Read-only. See [docs/phase3.md](../docs/phase3.md).
Phase 4 (built): deterministic fast path, incident to playbook to pull request. See [docs/phase4.md](../docs/phase4.md).

| File | Role |
| --- | --- |
| `detector.py` | Pure rules: raw Kubernetes JSON in, signals out. No I/O, no log text |
| `incidents.py` | Dedupes and correlates signals into one incident per failure; JSONL log |
| `collector.py` | The only code that talks to the cluster; diagnoser kubeconfig, refuses write-capable identities |
| `watch.py` | The loop: `python agent/watch.py [--once]` |
| `fastpath.py` | Reads the incident log, runs playbooks, validates, opens a PR with the fixer PAT. Never merges, no cluster access |
| `github.py` | Minimal GitHub client (branch, commit, PR, read PR) and the startup preflight (token not admin, main protected) |
| `gitrepo.py` | Read-only view of the repo at a fetched ref |
| `test_detection.py`, `test_fastpath.py` | Offline tests |

Context builder, diagnosis and fix agents arrive in Phases 6-7.
