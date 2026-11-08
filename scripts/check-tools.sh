#!/usr/bin/env bash
# Verify required tooling is installed and a cluster context is reachable.
missing=0
for t in docker kubectl flux helm kustomize kubeconform git; do
  if command -v "$t" >/dev/null 2>&1; then echo "ok       $t"; else echo "MISSING  $t"; missing=1; fi
done
kubectl cluster-info >/dev/null 2>&1 && echo "ok       cluster reachable" || echo "WARN     no reachable cluster"
exit $missing
