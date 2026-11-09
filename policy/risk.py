"""Policy / risk engine (Phase 5): maps a gated change to an autonomy level. Deterministic, no LLM.

    decide(verdict, meta, autonomy) -> {"level": "blocked"|"L0"|"L1"|..., "score": float, "reasons": [...], "ceiling": "L1"}

The effective level is the LOWER of (a) the ceiling for the fix class in policy/autonomy.yaml and (b) what the
risk score allows. A blocked or incomplete gate verdict is always 'blocked'. Phase 5 never returns above L1.
meta: {"playbook": str, "app": str, "files": {path: new text|None}, "old": {path: text|None}}
"""
import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ORDER = ["blocked", "L0", "L1", "L2", "L3"]


def load_autonomy(path=None):
    return yaml.safe_load(Path(path or HERE / "autonomy.yaml").read_text(encoding="utf-8"))


def _mem_values(text):
    return [int(m.group(1)) for m in re.finditer(r"\bmemory:\s*\"?(\d+)Mi", text or "")]


def decide(verdict, meta, autonomy=None):
    cfg = autonomy or load_autonomy()
    reasons = []
    if verdict.get("status") != "pass":
        why = verdict.get("failed") or verdict.get("skipped") or ["gate did not pass"]
        return {"level": "blocked", "score": None, "ceiling": None, "reasons": [f"gate {verdict.get('status')}: {', '.join(why)}"]}
    ceiling = cfg["classes"].get(meta.get("playbook"), cfg["default_level"])
    ceiling = ceiling if ceiling in ORDER else "L0"
    r = cfg["risk"]
    score = 0.0
    lines = verdict.get("changed_lines", 0)
    score += lines * r["per_changed_line"]
    reasons.append(f"{lines} changed line(s) x {r['per_changed_line']}")
    files = meta.get("files", {})
    old = meta.get("old", {})
    if any("/base/" in p for p in files):
        score += r["touches_base"]; reasons.append("touches shared base/")
    if any(t is None for t in files.values()):
        score += r["deletes_a_file"]; reasons.append("deletes a file")
    if cfg.get("apps", {}).get(meta.get("app")) == "high":
        score += r["high_criticality"]; reasons.append(f"{meta.get('app')} is high criticality")
    for p, new in files.items():
        o, n = _mem_values(old.get(p)), _mem_values(new)
        if o and n and max(n) > 2 * max(o):
            score += r["memory_change_over_2x"]; reasons.append("memory limit more than doubles")
    level = ceiling
    if score >= r["l0_at"] and ORDER.index(level) > ORDER.index("L0"):
        level = "L0"; reasons.append(f"score {score:.0f} >= {r['l0_at']}: downgraded to notify-only")
    # Phase 5 never grants more than L1, whatever the file says.
    if ORDER.index(level) > ORDER.index("L1"):
        level = "L1"; reasons.append("capped at L1 until Phase 9 promotes this class")
    return {"level": level, "score": round(score, 1), "ceiling": ceiling, "reasons": reasons}
