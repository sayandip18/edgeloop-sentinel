# watchdog

Phase 5. Host-level rollback watchdog (dead-man's switch), outside Kubernetes. Reverts a `sentinel(...)` change on `main` that is not confirmed healthy in time. Read-only cluster access (diagnoser kubeconfig); pushes the revert with the host's own git identity. See [docs/phase5.md](../docs/phase5.md).

    python watchdog/watchdog.py run --dry-run
    python watchdog/test_watchdog.py

Kill switch: create `watchdog/DISABLE` and it logs but never reverts.
