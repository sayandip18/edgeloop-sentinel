# infra/rbac

Scoped identities. Apply with `kubectl apply -f infra/rbac/` (the three YAML files).
Issue short-lived kubeconfigs with `bash scripts/make-kubeconfig.sh <diagnoser|fixer>`.
See docs/identities.md for the permission matrix.

These are applied **by hand with admin credentials, not by Flux**, on purpose: Flux's `deployer` identity must not be able to grant permissions (it has no RBAC rights), so letting Flux manage this directory would require widening it. `diagnoser.yaml` includes the Phase 6 Role that lets the diagnoser GET the Prometheus and Loki services (two named service:port pairs, nothing else). Re-apply after any change here; `scripts/verify-phase6.sh --cluster` checks the result against the live cluster.
Tokens from `make-kubeconfig.sh` expire. The responder and the diagnosis loop warn when the diagnoser token has under 10 minutes left; renew with `bash scripts/make-kubeconfig.sh diagnoser 8h`.
