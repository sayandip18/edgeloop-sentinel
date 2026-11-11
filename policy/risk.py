"""Policy / risk engine (Phase 5): maps a gated change to an autonomy level. Deterministic, no LLM.

    decide(verdict, meta, autonomy) -> {"level": "blocked"|"L0"|"L1"|..., "score": float, "reasons": [...], "ceiling": "L1"}

The effective level is the LOWER of (a) the ceiling for the fix class and (b) what the risk score allows. A blocked or
incomplete gate verdict is always 'blocked'. The ceiling is L1 at most when it comes from policy/autonomy.yaml; a class
reaches L2/L3 only through policy/promotions.json (Phase 9: written by policy/promotion.py from measured results, sealed,
and re-checked against its own evidence here on every call). L2/L3 also need a small risk score, so a promoted class
still goes to a human for a large or shared-base change.
meta: {"playbook": str, "app": str, "files": {path: new text|None}, "old": {path: text|None}}
"""
import re
from pathlib import Path

import yaml

import promotion

HERE = Path(__file__).resolve().parent
ORDER = ["blocked", "L0", "L1", "L2", "L3"]


def load_autonomy(path=None):
    return yaml.safe_load(Path(path or HERE / "autonomy.yaml").read_text(encoding="utf-8"))


def _mem_values(text):
    return [int(m.group(1)) for m in re.finditer(r"\bmemory:\s*\"?(\d+)Mi", text or "")]


def decide(verdict, meta, autonomy=None, levels=None):
    cfg = autonomy or load_autonomy()
    reasons = []
    if verdict.get("status") != "pass":
        why = verdict.get("failed") or verdict.get("skipped") or ["gate did not pass"]
        return {"level": "blocked", "score": None, "ceiling": None, "reasons": [f"gate {verdict.get('status')}: {', '.join(why)}"]}
    ceiling = cfg["classes"].get(meta.get("playbook"), cfg["default_level"])
    ceiling = ceiling if ceiling in ORDER else "L0"
    if ORDER.index(ceiling) > ORDER.index("L1"):          # a hand-written L2/L3 in autonomy.yaml is not a promotion
        reasons.append(f"autonomy.yaml says {ceiling}: ignored, only measured promotion may go above L1"); ceiling = "L1"
    if levels is None:                                    # tests may pass levels; otherwise the sealed, re-validated file
        levels, problems = promotion.load_levels(cfg)
        reasons.extend(problems)
    promoted = levels.get(meta.get("playbook"))
    if promoted in ("L2", "L3") and ORDER.index(ceiling) >= ORDER.index("L1"):
        ceiling = promoted; reasons.append(f"promoted to {promoted} by measured results")
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
    for lvl, key in (("L3", "l3_max_score"), ("L2", "l2_max_score")):     # a promoted class still needs a human for a big change
        if level == lvl and score > r.get(key, 0):
            level = "L2" if lvl == "L3" and score <= r.get("l2_max_score", 0) else "L1"
            reasons.append(f"score {score:.0f} > {r.get(key, 0)}: a human reviews this one ({level})")
    return {"level": level, "score": round(score, 1), "ceiling": ceiling, "reasons": reasons}
