#!/usr/bin/env bash
# Issue a short-lived kubeconfig for an identity: diagnoser | fixer
# Usage: bash scripts/make-kubeconfig.sh <identity> [duration, default 1h]
# Output: kubeconfigs/<identity>.kubeconfig (gitignored)
set -euo pipefail
cd "$(dirname "$0")/.."

ID="${1:-}"; DURATION="${2:-1h}"
CTX="${EDGELOOP_CONTEXT:-docker-desktop}"
case "$ID" in
  diagnoser|fixer) ;;
  deployer) echo "deployer is only used by Flux; no kubeconfig is issued." >&2; exit 1 ;;
  *) echo "usage: $0 <diagnoser|fixer> [duration]" >&2; exit 1 ;;
esac

TOKEN=$(kubectl --context "$CTX" -n observability create token "$ID" --duration "$DURATION")
SERVER=$(kubectl --context "$CTX" config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl --context "$CTX" config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

mkdir -p kubeconfigs
OUT="kubeconfigs/$ID.kubeconfig"
cat > "$OUT" <<KCFG
apiVersion: v1
kind: Config
clusters:
- name: edgeloop
  cluster:
    server: $SERVER
    certificate-authority-data: $CA
users:
- name: $ID
  user:
    token: $TOKEN
contexts:
- name: $ID@edgeloop
  context: {cluster: edgeloop, user: $ID}
current-context: $ID@edgeloop
KCFG
chmod 600 "$OUT" 2>/dev/null || true
echo "wrote $OUT (token valid $DURATION)"
