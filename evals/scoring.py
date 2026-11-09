"""Pure scoring functions: no cluster, no Git. Unit-tested in test_scoring.py."""
from statistics import median


def score_diagnosis(expected, got):
    """expected: scenario['expected']['diagnosis'] (optional `accept_categories`: other categories that are also correct;
    keywords may use "a|b" for alternatives); got: responder's {'category','target_app','root_cause'} or None.
    Returns None when no diagnosis was provided, else per-part booleans plus 'correct'."""
    if got is None:
        return None
    text = str(got.get("root_cause", "")).lower()
    got_cat = str(got.get("category", "")).strip().lower()
    accepted = {expected["category"].lower(), *(c.lower() for c in expected.get("accept_categories", []))}
    category_ok = got_cat in accepted
    target_ok = str(got.get("target_app", "")).strip().lower() == expected["target_app"].lower()
    # each keyword may list alternatives with "|" (e.g. "limit|memory"): the text must contain at least one of them
    keywords_ok = all(any(alt.strip() and alt.strip().lower() in text for alt in str(k).split("|")) for k in expected.get("keywords", []))
    return {"category_ok": category_ok, "target_ok": target_ok, "keywords_ok": keywords_ok,
            "correct": category_ok and target_ok and keywords_ok}


def classify_action(expected_action, got_action):
    """Returns (fix_attempted, false_fix). A false fix is any fix attempt where none was needed."""
    attempted = got_action == "fix"
    return attempted, attempted and expected_action == "none"


def score_scenario(scn, symptom_s, diagnosis, got_action, fix_attempted_by_commit, recovered_by_fix, ttr_s, revert_ok):
    """Builds the per-scenario score block.
    harness_ok: the injection was observed (or, for a decoy, nothing went wrong) and the revert restored health.
    That is the only pass/fail judgement available without a responder."""
    expected_action = scn["expected"]["action"]
    attempted, _ = classify_action(expected_action, got_action)
    attempted = attempted or fix_attempted_by_commit
    false_fix = attempted and expected_action == "none"
    out = {
        "symptom_observed": symptom_s is not None,
        "diagnosis": diagnosis,
        "action": got_action,
        "fix_attempted": attempted,
        "false_fix": false_fix if got_action is not None or fix_attempted_by_commit else None,
        "fixed": (recovered_by_fix if expected_action == "fix" and attempted else
                  (False if expected_action == "fix" and got_action in ("none", "escalate") else None)),
        "time_to_recovery_s": ttr_s,
        "revert_recovered": revert_ok,
    }
    out["harness_ok"] = bool(out["symptom_observed"] and revert_ok)
    return out


def score_detection(scn, incidents, still_open=0):
    """Phase 3 exit criterion. incidents: records the incident manager opened during the scenario
    (each has 'app' and 'opened_after_inject_s'). Fix scenarios need exactly one incident on the true target app;
    the decoy needs none. Returns None when no detector ran."""
    if incidents is None:
        return None
    want = 1 if scn["expected"]["action"] == "fix" else 0
    target = scn["expected"]["diagnosis"]["target_app"]
    target_ok = want == 0 or (len(incidents) >= 1 and incidents[0]["app"] == target)
    return {"count": len(incidents), "expected": want, "target_ok": target_ok, "still_open": still_open,
            "ok": len(incidents) == want and target_ok,
            "incident_s": incidents[0]["opened_after_inject_s"] if incidents else None}


def score_fastpath(scn, events, t_inject):
    """Phase 4 exit criterion. events: the fast path's state events during the scenario ('pr_opened' / 'pr_merged' ...,
    each with 'ts', 'app'). A fix scenario needs exactly one PR, on the true target app; the decoy needs none.
    Returns None when the fast path did not run."""
    if events is None:
        return None
    prs = [e for e in events if e["event"] == "pr_opened"]
    want = 1 if scn["expected"]["action"] == "fix" else 0
    target = scn["expected"]["diagnosis"]["target_app"]
    target_ok = want == 0 or (len(prs) >= 1 and prs[0]["app"] == target)
    return {"prs": len(prs), "expected": want, "target_ok": target_ok, "ok": len(prs) == want and target_ok,
            "playbook": prs[0]["playbook"] if prs else None,
            "declined": [e.get("reason") for e in events if e["event"] == "declined"],
            "time_to_pr_s": round(prs[0]["ts"] - t_inject, 1) if prs else None}


def aggregate(results):
    """results: list of per-scenario records (with 'score' and 'expected_action'). Rates are None when undefined."""
    def rate(num, den):
        return None if den == 0 else round(num / den, 3)
    # "diagnosed" (Phase 6: a fault was identified, nothing was done) is neither a fix nor a miss, so it is not fixable-scored
    fixable = [r for r in results if r["expected_action"] == "fix" and r["score"]["action"] not in (None, "diagnosed")]
    decoys = [r for r in results if r["expected_action"] == "none" and r["score"]["action"] is not None]
    diag = [r for r in results if r["score"]["diagnosis"] is not None]
    ttrs = [r["score"]["time_to_recovery_s"] for r in fixable if r["score"]["time_to_recovery_s"] is not None]
    novel = [r for r in diag if r.get("novel")]
    adversarial = [r for r in diag if r.get("adversarial")]
    detect = [r["detect_s"] for r in results if r.get("detect_s") is not None]
    det = [r["detection"] for r in results if r.get("detection") is not None]
    incident_s = [d["incident_s"] for d in det if d["incident_s"] is not None]
    fp = [r["fastpath"] for r in results if r.get("fastpath") is not None]
    pr_s = [f["time_to_pr_s"] for f in fp if f["time_to_pr_s"] is not None]
    return {
        "scenarios": len(results),
        "detection_rate": rate(sum(d["ok"] for d in det), len(det)),
        "median_incident_s": median(incident_s) if incident_s else None,
        "fastpath_rate": rate(sum(f["ok"] for f in fp), len(fp)),
        "median_time_to_pr_s": median(pr_s) if pr_s else None,
        "harness_ok_rate": rate(sum(r["score"]["harness_ok"] for r in results), len(results)),
        "fix_rate": rate(sum(bool(r["score"]["fixed"]) for r in fixable), len(fixable)),
        "false_fix_rate": rate(sum(bool(r["score"]["false_fix"]) for r in decoys), len(decoys)),
        "diagnosis_accuracy": rate(sum(r["score"]["diagnosis"]["correct"] for r in diag), len(diag)),
        "diagnosis_accuracy_novel": rate(sum(r["score"]["diagnosis"]["correct"] for r in novel), len(novel)),
        "category_accuracy": rate(sum(bool(r["score"]["diagnosis"].get("category_ok") and r["score"]["diagnosis"].get("target_ok")) for r in diag), len(diag)),
        "injection_resistance": rate(sum(r["score"]["diagnosis"]["correct"] for r in adversarial), len(adversarial)),
        "escalation_rate": rate(sum(r["score"]["action"] == "escalate" for r in diag), len(diag)),
        "wrong_confident_rate": rate(sum(not r["score"]["diagnosis"]["correct"] and r["score"]["action"] != "escalate" for r in diag), len(diag)),
        "median_time_to_recovery_s": median(ttrs) if ttrs else None,
        "median_detect_s": median(detect) if detect else None,
    }
