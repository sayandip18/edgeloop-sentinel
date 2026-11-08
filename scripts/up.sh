#!/usr/bin/env bash
# Prepare the Docker Desktop cluster: verify context, create namespaces.
# Kubernetes itself is enabled in Docker Desktop settings, not by this script.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX="${EDGELOOP_CONTEXT:-docker-desktop}"

if ! kubectl config get-contexts -o name | grep -qx "$CTX"; then
  echo "ERROR: kube context '$CTX' not found. Enable Kubernetes in Docker Desktop." >&2
  exit 1
fi
kubectl --context "$CTX" wait --for=condition=Ready node --all --timeout=60s
kubectl --context "$CTX" apply -f infra/cluster/namespaces.yaml
kubectl --context "$CTX" get ns apps flux-system observability
