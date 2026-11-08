# infra/rbac

Scoped identities. Apply with `kubectl apply -f infra/rbac/` (the three YAML files).
Issue short-lived kubeconfigs with `bash scripts/make-kubeconfig.sh <diagnoser|fixer>`.
See docs/identities.md for the permission matrix.
