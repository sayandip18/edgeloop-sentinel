#!/usr/bin/env bash
# Build demo app images into the local Docker store (visible to Docker Desktop k8s).
set -euo pipefail
cd "$(dirname "$0")/.."
for dir in apps/*/app; do
  name=$(basename "$(dirname "$dir")")
  docker build -q -t "edgeloop/${name}:v1" "$dir"
done
