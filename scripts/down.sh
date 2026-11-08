#!/usr/bin/env bash
# Remove edgeloop-sentinel namespaces (and everything in them, including Flux
# if bootstrapped). Does NOT disable Kubernetes in Docker Desktop.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX="${EDGELOOP_CONTEXT:-docker-desktop}"

if [[ "${1:-}" != "--yes" ]]; then
  echo "This deletes namespaces apps, flux-system, observability on context '$CTX'."
  echo "Re-run with --yes to confirm."
  exit 1
fi
kubectl --context "$CTX" delete -f infra/cluster/namespaces.yaml --ignore-not-found --wait=true
