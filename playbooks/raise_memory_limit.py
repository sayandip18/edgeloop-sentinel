"""Playbook: raise a container's memory limit after OOMKilled, when there is no recent change to blame.

Lower priority than revert-recent-change (undoing the cause beats compensating for it). Raises the limit by
`mem_factor`, rounded up to `mem_step_mi`, never above `mem_cap_mi`; at the cap it declines so a human decides.
Only edits `limits.memory` in apps/<app>/base/deployment.yaml, and only when there is exactly one such value.
"""
import math
import re

NAME = "raise-memory-limit"
FILE = "apps/{app}/base/deployment.yaml"
FLOW = re.compile(r"(limits:\s*\{[^}\n]*?\bmemory:\s*)(\d+)(Mi|Gi)\b")
BLOCK = re.compile(r"(limits:[ \t]*\n(?:[ \t]+(?!memory:)[^\n]*\n)*?[ \t]+memory:[ \t]*)(\d+)(Mi|Gi)\b")


def to_mi(n, unit):
    return n * 1024 if unit == "Gi" else n


def bump(text, cfg):
    """Returns (new_text, old_mi, new_mi) or raises ValueError with the reason."""
    found = [m for rx in (FLOW, BLOCK) for m in rx.finditer(text)]
    if len(found) != 1:
        raise ValueError(f"expected exactly one limits.memory value, found {len(found)}")
    m = found[0]
    old = to_mi(int(m.group(2)), m.group(3))
    if old >= cfg.mem_cap_mi:
        raise ValueError(f"limit is already {old}Mi (cap {cfg.mem_cap_mi}Mi); needs a human")
    new = min(cfg.mem_cap_mi, math.ceil(old * cfg.mem_factor / cfg.mem_step_mi) * cfg.mem_step_mi)
    return text[:m.start(2)] + str(new) + "Mi" + text[m.end(3):], old, new


def plan(incident, repo, cfg):
    from . import Plan, app_prefix, signal_types
    if "oom_killed" not in signal_types(incident):
        return None, "no oom_killed signal"
    if not app_prefix(incident, repo):
        return None, "app not found under apps/ (or unsafe name)"
    app = incident["app"]
    path = FILE.format(app=app)
    text = repo.read(path)
    if text is None:
        return None, f"{path} not found"
    try:
        new_text, old, new = bump(text, cfg)
    except ValueError as e:
        return None, str(e)
    return Plan(
        playbook=NAME, app=app, files={path: new_text},
        title=f"sentinel({app}): raise memory limit {old}Mi -> {new}Mi after OOMKilled",
        rationale=(f"`{app}` was OOMKilled and no recent manifest change could be reverted. Raising the limit is a "
                   f"stop-gap: check whether the app really needs more memory or is leaking."),
        changes=[f"{path}: resources.limits.memory {old}Mi -> {new}Mi"],
        expect={"memory_limit_mi": new}), ""
