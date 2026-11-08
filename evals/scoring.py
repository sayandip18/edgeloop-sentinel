"""Pure scoring functions: no cluster, no Git. Unit-tested in test_scoring.py."""
from statistics import median


def score_diagnosis(expected, got):
    """expected: scenario['expected']['diagnosis']; got: responder's {'category','target_app','root_cause'} or None.
    Returns None when no diagnosis was provided, else per-part booleans plus 'correct'."""
    if got is None:
        return None
    text = str(got.get("root_cause", "")).lower()
    category_ok = str(got.get("category", "")).strip().lower() == expected["category"].lower()
    target_ok = str(got.get("target_app", "")).strip().lower() == expected["target_app"].lower()
    keywords_ok = all(k.lower() in text for k in expected.get("keywords", []))
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
                  (False if expected_action == "fix" and got_action is not None else None)),
        "time_to_recovery_s": ttr_s,
        "revert_recovered": revert_ok,
    }
    out["harness_ok"] = bool(out["symptom_observed"] and revert_ok)
    return out


def aggregate(results):
    """results: list of per-scenario records (with 'score' and 'expected_action'). Rates are None when undefined."""
    def rate(num, den):
        return None if den == 0 else round(num / den, 3)
    fixable = [r for r in results if r["expected_action"] == "fix" and r["score"]["action"] is not None]
    decoys = [r for r in results if r["expected_action"] == "none" and r["score"]["action"] is not None]
    diag = [r for r in results if r["score"]["diagnosis"] is not None]
    ttrs = [r["score"]["time_to_recovery_s"] for r in fixable if r["score"]["time_to_recovery_s"] is not None]
    detect = [r["detect_s"] for r in results if r.get("detect_s") is not None]
    return {
        "scenarios": len(results),
        "harness_ok_rate": rate(sum(r["score"]["harness_ok"] for r in results), len(results)),
        "fix_rate": rate(sum(bool(r["score"]["fixed"]) for r in fixable), len(fixable)),
        "false_fix_rate": rate(sum(bool(r["score"]["false_fix"]) for r in decoys), len(decoys)),
        "diagnosis_accuracy": rate(sum(r["score"]["diagnosis"]["correct"] for r in diag), len(diag)),
        "median_time_to_recovery_s": median(ttrs) if ttrs else None,
        "median_detect_s": median(detect) if detect else None,
    }
