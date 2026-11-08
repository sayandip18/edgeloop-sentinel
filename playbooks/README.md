# playbooks

Deterministic remediations (Phase 4). No LLM, no cluster access. A playbook reads one incident record and a read-only view of the repo, and returns either a **plan** (exact new file contents) or a **reason it declined**. `agent/fastpath.py` validates the plan and opens the pull request. See [docs/phase4.md](../docs/phase4.md).

| Playbook | Fires when | Does |
| --- | --- | --- |
| `revert_recent_change.py` (priority 1) | the latest commit touching `apps/<app>/` landed shortly before the incident, only modified `base/` or `overlays/` files, is not itself a revert or sentinel fix, and nothing changed those files since | restores those files to their content before that commit |
| `raise_memory_limit.py` (priority 2) | `oom_killed` signal and no recent change to revert | raises `limits.memory` in `base/deployment.yaml` by 1.5x (rounded up to 64Mi, cap 512Mi); at the cap it declines |

Adding a playbook: a module with `NAME` and `plan(incident, repo, cfg) -> (Plan | None, reason)`, registered in `playbooks/__init__.py::select`, with a test in `agent/test_fastpath.py`. A playbook must stay inside `apps/<app>/`, must be exact and deterministic, and must decline whenever it is unsure. Declining escalates (Phase 8 pages a human).
