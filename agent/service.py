"""Shared entry point for one diagnosis (Phase 6): used by the eval responder and by the diagnosis loop.

Builds the evidence bundle, runs the agent, and (when there is a real incident) records a `diagnosed` event.
Read-only: collector (diagnoser kubeconfig) and a read-only Git view. No writes except the append to the incident log.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from context import ContextBuilder  # noqa: E402
from diagnose import Diagnoser, Toolbox, record_diagnosis  # noqa: E402


def run_diagnosis(col, repo, incident, focus_app, *, min_confidence=0.6, budget=None, client=None, model=None,
                  incidents_log=None, record=True, now=None):
    """Returns the agent's result dict (action, reason, diagnosis, tool_calls, tokens, model, error, seconds)."""
    snap = col.snapshot()
    bundle, ledger = ContextBuilder(col, repo).build(incident, focus_app=focus_app)
    res = Diagnoser(client=client, model=model, min_confidence=min_confidence, budget=budget).diagnose(
        bundle, ledger, Toolbox(col, repo, ledger, snap))
    if record and incidents_log and incident and incident.get("id"):
        record_diagnosis(incidents_log, incident["id"], res, now=now)
    return res
