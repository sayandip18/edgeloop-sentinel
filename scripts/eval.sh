#!/usr/bin/env bash
# Thin wrapper (make is not installed):  bash scripts/eval.sh list | validate | run <id...|all> [--yes] [--dry-run] [--responder CMD]
cd "$(dirname "$0")/.." && exec python evals/run.py "$@"
