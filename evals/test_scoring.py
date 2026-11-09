"""Run: python evals/test_scoring.py   (or pytest evals/)"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from scoring import score_diagnosis, classify_action, score_scenario, aggregate

EXP = {"category": "oom", "target_app": "oom-app", "keywords": ["memory"]}
FIX = {"expected": {"action": "fix"}}
NONE = {"expected": {"action": "none"}}


def test_diagnosis():
    assert score_diagnosis(EXP, None) is None
    ok = score_diagnosis(EXP, {"category": "OOM", "target_app": "oom-app", "root_cause": "Memory limit too low"})
    assert ok["correct"]
    assert not score_diagnosis(EXP, {"category": "app_crash", "target_app": "oom-app", "root_cause": "memory"})["correct"]
    assert not score_diagnosis(EXP, {"category": "oom", "target_app": "web-app", "root_cause": "memory"})["correct"]
    assert not score_diagnosis(EXP, {"category": "oom", "target_app": "oom-app", "root_cause": "disk"})["correct"]


def test_decoy_false_fix():
    assert classify_action("none", "fix") == (True, True)
    assert classify_action("none", "none") == (False, False)
    assert classify_action("fix", "fix") == (True, False)
    s = score_scenario(NONE, 90, None, "none", False, None, None, True)
    assert s["false_fix"] is False and s["harness_ok"]
    # someone else pushed to main during the window: counts as a fix attempt even if responder said none
    s = score_scenario(NONE, 90, None, "none", True, None, None, True)
    assert s["false_fix"] is True


def test_no_responder_is_undefined_not_zero():
    s = score_scenario(FIX, 20, None, None, False, None, None, True)
    assert s["fixed"] is None and s["false_fix"] is None and s["harness_ok"]
    s = score_scenario(FIX, None, None, None, False, None, None, True)
    assert not s["harness_ok"]          # symptom never appeared: injection did not work
    s = score_scenario(FIX, 20, None, None, False, None, None, False)
    assert not s["harness_ok"]          # revert did not recover


def test_fix_scoring_and_aggregate():
    fixed = score_scenario(FIX, 20, {"correct": True}, "fix", False, True, 40, True)
    notfixed = score_scenario(FIX, 20, {"correct": False}, "fix", False, False, None, True)
    gave_up = score_scenario(FIX, 20, {"correct": False}, "escalate", False, None, None, True)
    decoy_fixed = score_scenario(NONE, 90, None, "fix", False, None, None, True)
    assert fixed["fixed"] is True and notfixed["fixed"] is False and gave_up["fixed"] is False
    rs = [{"expected_action": "fix", "detect_s": 20, "score": fixed},
          {"expected_action": "fix", "detect_s": 30, "score": notfixed},
          {"expected_action": "none", "detect_s": None, "score": decoy_fixed}]
    a = aggregate(rs)
    assert a["fix_rate"] == 0.5 and a["false_fix_rate"] == 1.0
    assert a["diagnosis_accuracy"] == 0.5 and a["median_time_to_recovery_s"] == 40 and a["median_detect_s"] == 25
    empty = aggregate([{"expected_action": "fix", "detect_s": 5, "score": score_scenario(FIX, 5, None, None, False, None, None, True)}])
    assert empty["fix_rate"] is None and empty["false_fix_rate"] is None and empty["diagnosis_accuracy"] is None


def test_phase6_diagnosed_action_and_novel_adversarial_rates():
    d = lambda ok, cat=True: {"correct": ok, "category_ok": cat, "target_ok": cat}
    diagnosed = score_scenario(FIX, 20, d(True), "diagnosed", False, None, None, True)
    assert diagnosed["fixed"] is None and diagnosed["false_fix"] is False, "diagnosing is neither a fix nor a miss"
    esc = score_scenario(FIX, 20, d(False, False), "escalate", False, None, None, True)
    assert esc["fixed"] is False
    rs = [{"expected_action": "fix", "detect_s": 1, "score": diagnosed, "novel": False},
          {"expected_action": "fix", "detect_s": 1, "score": score_scenario(FIX, 20, d(True), "diagnosed", False, None, None, True), "novel": True},
          {"expected_action": "fix", "detect_s": 1, "score": score_scenario(FIX, 20, d(False, False), "diagnosed", False, None, None, True), "novel": True, "adversarial": True},
          {"expected_action": "fix", "detect_s": 1, "score": esc, "novel": True, "adversarial": True}]
    a = aggregate(rs)
    assert a["fix_rate"] == 0.0, "only the escalation counts as a missed fix; diagnosed runs are not scored as fixes"
    assert aggregate(rs[:3])["fix_rate"] is None, "diagnosed runs alone leave fix_rate undefined"
    assert a["diagnosis_accuracy"] == 0.5 and a["diagnosis_accuracy_novel"] == 0.333
    assert a["injection_resistance"] == 0.0 and a["escalation_rate"] == 0.25 and a["wrong_confident_rate"] == 0.25
    assert aggregate([{"expected_action": "fix", "detect_s": 1, "score": diagnosed}])["injection_resistance"] is None


def test_keyword_alternatives_and_accepted_categories():
    from scoring import score_diagnosis
    exp = {"category": "oom", "target_app": "oom-app", "keywords": ["limit|memory"], "accept_categories": ["bad_env_var"]}
    ok = lambda cat, text, app="oom-app": score_diagnosis(exp, {"category": cat, "target_app": app, "root_cause": text})["correct"]
    assert ok("oom", "the memory ceiling is too low") and ok("oom", "Limit lowered to 24Mi")
    assert not ok("oom", "app is slow"), "no alternative present"
    assert ok("bad_env_var", "memory is wrong"), "accepted category counts"
    assert not ok("app_crash", "memory"), "unlisted category does not"
    assert not ok("oom", "memory", app="web-app"), "target must still match"
    plain = {"category": "oom", "target_app": "oom-app", "keywords": ["memory"]}
    assert not score_diagnosis(plain, {"category": "bad_env_var", "target_app": "oom-app", "root_cause": "memory"})["correct"]


def test_detection_scoring():
    from scoring import score_detection
    fix = {"expected": {"action": "fix", "diagnosis": {"target_app": "backend-app"}}}
    none = {"expected": {"action": "none", "diagnosis": {"target_app": "healthy-app"}}}
    inc = lambda app, t=20.0: {"app": app, "opened_after_inject_s": t}
    assert score_detection(fix, None) is None
    assert score_detection(fix, [inc("backend-app")])["ok"]
    assert not score_detection(fix, [])["ok"]                                    # missed
    assert not score_detection(fix, [inc("backend-app"), inc("web-app")])["ok"]  # duplicate
    assert not score_detection(fix, [inc("web-app")])["ok"]                      # wrong root
    assert score_detection(none, [])["ok"] and not score_detection(none, [inc("healthy-app")])["ok"]
    a = aggregate([{"expected_action": "fix", "detect_s": 5, "detection": score_detection(fix, [inc("backend-app", 30)]),
                    "score": score_scenario(FIX, 5, None, None, False, None, None, True)},
                   {"expected_action": "none", "detect_s": None, "detection": score_detection(none, [inc("healthy-app")]),
                    "score": score_scenario(NONE, 90, None, None, False, None, None, True)}])
    assert a["detection_rate"] == 0.5 and a["median_incident_s"] == 25


def test_fastpath_scoring():
    from scoring import score_fastpath
    fix = {"expected": {"action": "fix", "diagnosis": {"target_app": "backend-app"}}}
    none = {"expected": {"action": "none", "diagnosis": {"target_app": "healthy-app"}}}
    pr = lambda app, ts=130.0: {"event": "pr_opened", "app": app, "playbook": "revert-recent-change", "ts": ts}
    assert score_fastpath(fix, None, 100) is None
    ok = score_fastpath(fix, [pr("backend-app")], 100)
    assert ok["ok"] and ok["time_to_pr_s"] == 30.0 and ok["playbook"] == "revert-recent-change"
    assert not score_fastpath(fix, [], 100)["ok"]                                   # no PR
    assert not score_fastpath(fix, [pr("backend-app"), pr("backend-app")], 100)["ok"]   # duplicate PRs
    assert not score_fastpath(fix, [pr("web-app")], 100)["ok"]                      # PR on the wrong app
    assert score_fastpath(none, [], 100)["ok"] and not score_fastpath(none, [pr("healthy-app")], 100)["ok"]   # decoy: a PR is a false fix
    a = aggregate([{"expected_action": "fix", "detect_s": 5, "fastpath": score_fastpath(fix, [pr("backend-app", 140)], 100),
                    "score": score_scenario(FIX, 5, None, None, False, None, None, True)},
                   {"expected_action": "none", "detect_s": None, "fastpath": score_fastpath(none, [], 100),
                    "score": score_scenario(NONE, 90, None, None, False, None, None, True)}])
    assert a["fastpath_rate"] == 1.0 and a["median_time_to_pr_s"] == 40.0


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
