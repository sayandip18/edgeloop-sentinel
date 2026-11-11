#!/usr/bin/env python3
"""Graduated autonomy (Phase 9): promote or demote each fix class from MEASURED results only.

    python policy/promotion.py status   [--results GLOB ...]      # evidence and the level each class would get
    python policy/promotion.py apply    [--results GLOB ...]      # write policy/promotions.json (one step per run) and audit it
    python policy/promotion.py verify                             # the file is sealed, and its evidence matches the sources

Evidence per fix class, all deterministic:
- eval trials: records in evals/results/*.json (live runs of the Phase 2 harness with the fast path or fix agent) where
  that class opened a PR. A trial succeeds when the scenario expected a fix and it recovered; a PR on a decoy is a
  false fix. Only results inside `window_days` count, so a regression shows up and an old false fix ages out.
- human decisions: approvals (merged PRs) and standing rejections (closed PRs) from the Phase 8 feedback store. A PR the
  merger itself merged is NOT an approval: only a human merge counts as one.
- outcomes of earlier automatic merges: `auto_merge_ineffective` records in the audit log.

Rules (thresholds live in policy/autonomy.yaml under `promotion:`; nothing here is hand-tuned per class)
- L1 -> L2 and L2 -> L3 need the thresholds for the target level, one step per run.
- A class at L2/L3 that no longer meets the thresholds for its current level is demoted one step: a false fix, a standing
  rejection, an ineffective auto-merge, a lower fix rate, or evidence that aged out of the window.
- Classes in `promotion.excluded` never leave L1. Classes at L0 are never promoted.
- Nobody edits levels by hand: autonomy.yaml cannot grant above L1 (risk.py caps it), and promotions.json is sealed and
  carries its evidence. risk.py re-checks every entry against the thresholds each time and clamps one that fails to L1.
"""
import argparse
import glob
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "agent"))
from controls import AUDIT_FILE, Audit  # noqa: E402

LEVELS_FILE = HERE / "promotions.json"
FEEDBACK_FILE = ROOT / "agent" / "state" / "feedback.jsonl"
RESULTS_GLOB = str(ROOT / "evals" / "results" / "*.json")
RANK = {"blocked": -1, "L0": 0, "L1": 1, "L2": 2, "L3": 3}


def load_cfg(path=None):
    return yaml.safe_load(Path(path or HERE / "autonomy.yaml").read_text(encoding="utf-8"))


# --- evidence ------------------------------------------------------------------------------------------------------------
def _blank():
    return {"trials": 0, "scenarios": 0, "successes": 0, "false_fixes": 0, "fix_rate": None,
            "approvals": 0, "rejections": 0, "ineffective_merges": 0}


def gather(classes, results_files, feedback_events, audit_events, window_days, now=None):
    """Evidence for each class name in `classes`. Pure: callers hand in already-read data."""
    import feedback as fb
    now = time.time() if now is None else now
    ev = {c: _blank() for c in classes}
    seen = {c: set() for c in classes}
    for path in results_files:
        p = Path(path)
        try:
            if now - p.stat().st_mtime > window_days * 86400:
                continue
            records = json.loads(p.read_text(encoding="utf-8")).get("results", [])
        except (OSError, ValueError):
            continue
        for r in records:
            f = r.get("fastpath") or {}
            cls = f.get("playbook")
            if cls not in ev or not f.get("prs"):
                continue
            e = ev[cls]
            e["trials"] += 1
            seen[cls].add(r.get("id"))
            if r.get("expected_action") == "none":
                e["false_fixes"] += 1
            elif (r.get("score") or {}).get("fixed"):
                e["successes"] += 1
    auto_merged = {x.get("pr") for x in audit_events if x.get("action") == "auto_merge" and x.get("outcome") == "merged"}
    # standing rejections: the latest decision per (signature, family), exactly as the feedback store resolves it
    standing = {}
    for x in feedback_events:
        if x.get("event") in ("rejected", "approved", "forgotten") and x.get("signature") and x.get("fix_class"):
            standing[(x["signature"], fb.family(x["fix_class"]))] = x
    for c in ev:
        ev[c]["rejections"] = sum(1 for (s, fam), x in standing.items() if fam == fb.family(c) and x["event"] == "rejected")
        ev[c]["approvals"] = sum(1 for x in feedback_events if x.get("event") == "approved" and x.get("fix_class") == c
                                 and x.get("pr") not in auto_merged and now - x.get("ts", now) <= 90 * 86400)
        ev[c]["ineffective_merges"] = sum(1 for x in audit_events if x.get("action") == "auto_merge_ineffective"
                                          and x.get("fix_class") == c and now - x["ts"] <= window_days * 86400)
        ev[c]["scenarios"] = len(seen[c])
        t = ev[c]["trials"]
        ev[c]["fix_rate"] = round(ev[c]["successes"] / t, 3) if t else None
    return ev


def unmet(level, ev, cfg):
    """Why this evidence does not support `level` (empty list = it does). L0/L1 need nothing."""
    need = (cfg.get("promotion") or {}).get(level)
    if not need:
        return [] if level in ("L0", "L1") else [f"no thresholds defined for {level}"]
    out = []
    if ev["trials"] < need["min_trials"]: out.append(f"trials {ev['trials']} < {need['min_trials']}")
    if ev["scenarios"] < need["min_scenarios"]: out.append(f"distinct scenarios {ev['scenarios']} < {need['min_scenarios']}")
    if ev["fix_rate"] is None or ev["fix_rate"] < need["min_fix_rate"]: out.append(f"fix rate {ev['fix_rate']} < {need['min_fix_rate']}")
    if ev["false_fixes"] > need["max_false_fixes"]: out.append(f"false fixes {ev['false_fixes']} > {need['max_false_fixes']}")
    if ev["approvals"] < need["min_approvals"]: out.append(f"human approvals {ev['approvals']} < {need['min_approvals']}")
    if ev["rejections"]: out.append(f"{ev['rejections']} standing human rejection(s)")
    if ev["ineffective_merges"]: out.append(f"{ev['ineffective_merges']} auto-merge(s) did not resolve their incident")
    return out


def step(cls, current, ev, cfg):
    """One decision: (new_level, reasons). At most one step up or down."""
    base = (cfg.get("classes") or {}).get(cls, cfg.get("default_level", "L0"))
    excluded = cls in ((cfg.get("promotion") or {}).get("excluded") or [])
    current = current if current in RANK else "L1"
    if RANK.get(base, 0) < 1:
        return "L0" if base == "L0" else "L1", ["class is notify-only (L0) in autonomy.yaml: never promoted"]
    if excluded:
        return "L1", ["excluded from promotion in autonomy.yaml"]
    if RANK[current] > 1:
        miss = unmet(current, ev, cfg)
        if miss:
            return f"L{RANK[current] - 1}", ["demoted: " + "; ".join(miss)]
    if RANK[current] < 3:
        nxt = f"L{RANK[current] + 1}"
        miss = unmet(nxt, ev, cfg)
        if not miss:
            return nxt, [f"promoted: meets every {nxt} threshold"]
        return current, [f"stays {current}; for {nxt}: " + "; ".join(miss)]
    return current, [f"stays {current}"]


# --- the sealed levels file ----------------------------------------------------------------------------------------------
def _seal(classes):
    body = json.dumps(classes, sort_keys=True, separators=(",", ":")).encode()
    key = os.environ.get("EDGELOOP_PROMOTION_KEY")
    return hmac.new(key.encode(), body, "sha256").hexdigest() if key else hashlib.sha256(body).hexdigest()


def write_levels(classes, path=LEVELS_FILE):
    Path(path).write_text(json.dumps({"evaluated_at": time.time(), "classes": classes, "seal": _seal(classes)}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_levels(cfg=None, path=LEVELS_FILE):
    """({class: level}, [problems]). Only levels whose evidence still meets the thresholds survive; a broken seal voids all of
    them. A missing file is normal (nothing promoted yet)."""
    p = Path(path)
    if not p.exists():
        return {}, []
    cfg = cfg or load_cfg()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        classes = d["classes"]
    except (ValueError, KeyError):
        return {}, ["promotions.json is unreadable: every class stays at L1"]
    if not hmac.compare_digest(str(d.get("seal", "")), _seal(classes)):
        return {}, ["promotions.json seal does not match its content (hand-edited?): every class stays at L1"]
    out, problems = {}, []
    excluded = (cfg.get("promotion") or {}).get("excluded") or []
    for c, e in classes.items():
        lvl = e.get("level")
        if RANK.get(lvl, 0) <= 1:
            continue
        miss = unmet(lvl, e.get("evidence") or _blank(), cfg)
        if c in excluded: miss.append("class is excluded from promotion")
        if (cfg.get("classes") or {}).get(c) != "L1": miss.append("class is not at L1 in autonomy.yaml")
        if miss:
            problems.append(f"{c}: recorded {lvl} is not supported by its own evidence ({'; '.join(miss)}): clamped to L1")
        else:
            out[c] = lvl
    return out, problems


def current_levels(path=LEVELS_FILE):
    try:
        return {c: e["level"] for c, e in json.loads(Path(path).read_text(encoding="utf-8"))["classes"].items()}
    except (OSError, ValueError, KeyError):
        return {}


# --- driver --------------------------------------------------------------------------------------------------------------
def _read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def evaluate(cfg, results_globs, feedback_path, audit, levels_path=LEVELS_FILE, now=None):
    files = sorted({f for g in results_globs for f in glob.glob(g)})
    classes = list((cfg.get("classes") or {}))
    ev = gather(classes, files, _read_jsonl(feedback_path), audit.events(), (cfg.get("promotion") or {}).get("window_days", 30), now)
    cur = current_levels(levels_path)
    rows = {}
    for c in classes:
        lvl, why = step(c, cur.get(c, "L1"), ev[c], cfg)
        rows[c] = {"before": cur.get(c, "L1"), "level": lvl, "reasons": why, "evidence": ev[c]}
    return rows, files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "apply", "verify"])
    ap.add_argument("--results", action="append", help="eval result glob (repeatable; default evals/results/*.json)")
    ap.add_argument("--feedback", default=str(FEEDBACK_FILE))
    ap.add_argument("--audit", default=str(AUDIT_FILE))
    ap.add_argument("--levels", default=str(LEVELS_FILE))
    a = ap.parse_args()
    cfg, audit = load_cfg(), Audit(a.audit)
    rows, files = evaluate(cfg, a.results or [RESULTS_GLOB], a.feedback, audit, a.levels)
    if a.cmd == "verify":
        lv, problems = load_levels(cfg, a.levels)
        try:
            recorded = json.loads(Path(a.levels).read_text(encoding="utf-8"))["classes"] if Path(a.levels).exists() else {}
        except (ValueError, KeyError):
            recorded = {}
        for c, e in recorded.items():
            if c in rows and e.get("evidence") != rows[c]["evidence"]:
                problems.append(f"{c}: recorded evidence differs from what the sources show now (rerun `apply`, or the file was forged)")
        for p in problems:
            print("PROBLEM", p)
        print(f"{len(recorded)} recorded class(es), {len(lv)} above L1; " + ("consistent" if not problems else "NOT consistent"))
        return 1 if problems else 0
    print(f"{len(files)} eval result file(s) read\n")
    print(f"{'class':<26}{'now':<5}{'next':<5}{'trials':<8}{'scen':<6}{'fix':<7}{'false':<7}{'appr':<6}{'rej':<5}why")
    for c, r in rows.items():
        e = r["evidence"]
        print(f"{c:<26}{r['before']:<5}{r['level']:<5}{e['trials']:<8}{e['scenarios']:<6}{str(e['fix_rate']):<7}{e['false_fixes']:<7}{e['approvals']:<6}{e['rejections']:<5}{'; '.join(r['reasons'])}")
    if a.cmd == "apply":
        try:
            old = json.loads(Path(a.levels).read_text(encoding="utf-8"))["classes"]
        except (OSError, ValueError, KeyError):
            old = {}
        out = {}
        for c, r in rows.items():
            since = time.time() if r["level"] != r["before"] else old.get(c, {}).get("since", time.time())
            out[c] = {"level": r["level"], "since": since, "evidence": r["evidence"], "reasons": r["reasons"]}
            if r["level"] != r["before"]:
                audit.append("promotion-engine", "promotion" if RANK[r["level"]] > RANK[r["before"]] else "demotion",
                             fix_class=c, before=r["before"], after=r["level"], evidence=r["evidence"], reasons=r["reasons"])
        write_levels(out, a.levels)
        print(f"\nwrote {a.levels} ({sum(1 for r in rows.values() if r['level'] != r['before'])} change(s)); commit it to make it the reviewed record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
