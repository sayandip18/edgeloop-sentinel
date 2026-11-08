"""Deterministic playbooks (Phase 4). No LLM, no cluster access.

A playbook looks at one incident record plus a read-only view of the repo and either returns a Plan (the exact
file contents to commit) or declines with a reason. Playbooks never write anything; `agent/fastpath.py` validates
the plan and opens the pull request. Order in REGISTRY is priority: the first playbook that produces a plan wins.
"""
import re
from dataclasses import dataclass, field


@dataclass
class Config:
    lookback_s: float = 3600.0    # a change older than this before the incident is not blamed
    slack_s: float = 120.0        # a commit may be this much *after* incident open (clock skew, flux lag)
    mem_factor: float = 1.5
    mem_step_mi: int = 64
    mem_cap_mi: int = 512


@dataclass
class Plan:
    playbook: str
    app: str
    files: dict                   # path -> full new text
    title: str
    rationale: str                # why this playbook, human readable
    changes: list = field(default_factory=list)   # one line per change
    expect: dict = field(default_factory=dict)    # extra checks for the validation build, e.g. {"memory_limit_mi": 192}


APP_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def app_prefix(incident, repo):
    """'apps/<app>/' for the incident's root app, or None if the name is unsafe or the app is not in the repo.
    The name comes from cluster labels, so it is checked before it is used in any path."""
    app = str(incident.get("app", ""))
    if not APP_RE.match(app):
        return None
    prefix = f"apps/{app}/"
    return prefix if repo.read(f"{prefix}base/kustomization.yaml") is not None else None


def signal_types(incident):
    return {s["type"] for s in incident.get("signals", [])}


def select(incident, repo, cfg=None):
    """Returns (plan | None, declines) where declines is [(playbook name, reason)] for every playbook that did not apply."""
    from . import raise_memory_limit, revert_recent_change
    cfg = cfg or Config()
    declines = []
    for pb in (revert_recent_change, raise_memory_limit):     # priority order
        plan, reason = pb.plan(incident, repo, cfg)
        if plan:
            return plan, declines
        declines.append((pb.NAME, reason))
    return None, declines
