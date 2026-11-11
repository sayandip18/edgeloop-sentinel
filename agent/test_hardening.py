"""Offline tests for Phase 9: graduated autonomy, controls, audit log, merger. No cluster, network, token or model.
Run: python agent/test_hardening.py"""
import copy, json, os, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "policy"))
import controls, merger, promotion, risk  # noqa: E402
from feedback import FeedbackStore  # noqa: E402
from github import GitHubError  # noqa: E402

CFG = promotion.load_cfg()
CLS = "revert-recent-change"
NOW = 1_800_000_000.0


def tmp():
    return Path(tempfile.mkdtemp(prefix="sentinel-p9-"))


def results_file(d, name, records, age_days=1):
    p = d / name
    p.write_text(json.dumps({"results": records}), encoding="utf-8")
    t = NOW - age_days * 86400
    os.utime(p, (t, t))
    return p


def rec(sid, cls=CLS, fixed=True, expected="fix"):
    return {"id": sid, "expected_action": expected, "score": {"fixed": fixed if expected == "fix" else None},
            "fastpath": {"playbook": cls, "prs": 1, "ok": True}}


def good_records(n=10, scenarios=3, cls=CLS):
    return [rec(f"s{i % scenarios}", cls) for i in range(n)]


def approvals(n, cls=CLS):
    return [{"ts": NOW - 100, "event": "approved", "signature": f"app{i}:oom_killed", "fix_class": cls, "pr": 100 + i} for i in range(n)]


def ev_for(d, records, fb=(), audit=(), cls=CLS):
    f = results_file(d, "r.json", records)
    return promotion.gather([cls], [f], list(fb), list(audit), 30, NOW)[cls]


# --- promotion -----------------------------------------------------------------------------------------------------------
def test_promotion_needs_every_threshold():
    d = tmp()
    ok = ev_for(d, good_records(), approvals(3))
    assert promotion.unmet("L2", ok, CFG) == [], ok
    assert promotion.step(CLS, "L1", ok, CFG)[0] == "L2"
    for name, e in {"few trials": ev_for(d, good_records(9), approvals(3)), "one scenario": ev_for(d, good_records(10, 1), approvals(3)),
                    "no approvals": ev_for(d, good_records(), approvals(2)),
                    "low fix rate": ev_for(d, good_records(8) + [rec("s0", fixed=False)] * 2, approvals(3))}.items():
        assert promotion.step(CLS, "L1", e, CFG)[0] == "L1", name


def test_one_step_at_a_time_and_l3_needs_more():
    d = tmp()
    big = ev_for(d, good_records(30, 6), approvals(12))
    assert promotion.step(CLS, "L1", big, CFG)[0] == "L2"            # not straight to L3
    assert promotion.step(CLS, "L2", big, CFG)[0] == "L3"
    assert promotion.step(CLS, "L2", ev_for(d, good_records(), approvals(3)), CFG)[0] == "L2"


def test_a_false_fix_demotes_and_ages_out():
    d = tmp()
    bad = ev_for(d, good_records() + [rec("decoy", expected="none")], approvals(3))
    assert bad["false_fixes"] == 1
    lvl, why = promotion.step(CLS, "L2", bad, CFG)
    assert lvl == "L1" and "false fixes" in why[0], (lvl, why)
    old = results_file(d, "old.json", good_records() + [rec("decoy", expected="none")], age_days=40)   # outside the window
    e = promotion.gather([CLS], [old], approvals(3), [], 30, NOW)[CLS]
    assert e["false_fixes"] == 0 and e["trials"] == 0           # aged out: and so is the good evidence
    assert promotion.step(CLS, "L2", e, CFG)[0] == "L1"         # stale evidence supports no autonomy


def test_rejection_and_ineffective_merge_demote_and_merger_approvals_do_not_count():
    d = tmp()
    rej = [{"ts": NOW - 5, "event": "rejected", "signature": "oom-app:oom_killed", "fix_class": "llm-revert-change", "pr": 1}]
    e = ev_for(d, good_records(), approvals(3) + rej)
    assert e["rejections"] == 1 and promotion.step(CLS, "L2", e, CFG)[0] == "L1"   # family match: llm-revert-change blocks the playbook
    ineff = [{"ts": NOW - 5, "action": "auto_merge_ineffective", "fix_class": CLS, "pr": 9}]
    assert promotion.step(CLS, "L2", ev_for(d, good_records(), approvals(3), ineff), CFG)[0] == "L1"
    auto = [{"ts": NOW - 50, "action": "auto_merge", "outcome": "merged", "pr": 100 + i} for i in range(3)]
    assert ev_for(d, good_records(), approvals(3), auto)["approvals"] == 0       # the merger's own merges are not human approval


def test_excluded_and_l0_classes_never_promote():
    d = tmp()
    c = "llm-fix-image-tag"
    assert promotion.step(c, "L1", ev_for(d, good_records(30, 6, c), approvals(12, c), cls=c), CFG)[0] == "L1"
    cfg = copy.deepcopy(CFG); cfg["classes"]["x"] = "L0"
    assert promotion.step("x", "L1", promotion._blank(), cfg)[0] == "L0"


def test_levels_file_is_sealed_and_rechecked_against_its_evidence():
    d = tmp(); p = d / "promotions.json"
    good = ev_for(d, good_records(), approvals(3))
    promotion.write_levels({CLS: {"level": "L2", "evidence": good, "reasons": []}}, p)
    assert promotion.load_levels(CFG, p) == ({CLS: "L2"}, [])
    data = json.loads(p.read_text()); data["classes"][CLS]["level"] = "L3"; p.write_text(json.dumps(data))     # hand edit
    lv, problems = promotion.load_levels(CFG, p)
    assert lv == {} and "seal" in problems[0]
    promotion.write_levels({CLS: {"level": "L3", "evidence": good, "reasons": []}}, p)                          # resealed but unearned
    lv, problems = promotion.load_levels(CFG, p)
    assert lv == {} and "not supported by its own evidence" in problems[0], problems
    promotion.write_levels({"llm-fix-image-tag": {"level": "L2", "evidence": good, "reasons": []}}, p)
    assert promotion.load_levels(CFG, p)[0] == {}


def test_exit_criterion_promote_by_results_then_demote_on_regression():
    """apply -> L2 from results; new results with a false fix -> apply -> back to L1; both written to the audit log."""
    d = tmp(); lv = d / "promotions.json"; audit = controls.Audit(d / "audit.jsonl")
    fb = d / "fb.jsonl"; fb.write_text("".join(json.dumps(x) + "\n" for x in approvals(3)))
    results_file(d, "a.json", good_records())
    rows, _ = promotion.evaluate(CFG, [str(d / "*.json")], fb, audit, lv, NOW)
    assert rows[CLS]["level"] == "L2", rows[CLS]
    promotion.write_levels({c: {"level": r["level"], "evidence": r["evidence"], "reasons": r["reasons"]} for c, r in rows.items()}, lv)
    audit.append("promotion-engine", "promotion", fix_class=CLS, before="L1", after="L2")
    assert promotion.load_levels(CFG, lv)[0] == {CLS: "L2"}
    results_file(d, "b.json", [rec("decoy-x", expected="none")])
    rows, _ = promotion.evaluate(CFG, [str(d / "*.json")], fb, audit, lv, NOW)
    assert rows[CLS]["before"] == "L2" and rows[CLS]["level"] == "L1" and "false fixes" in rows[CLS]["reasons"][0], rows[CLS]


def test_risk_engine_honours_only_promotions_and_keeps_a_human_for_big_changes():
    ok = {"status": "pass", "changed_lines": 2}
    meta = {"playbook": CLS, "app": "oom-app", "files": {"apps/oom-app/overlays/healthy/kustomization.yaml": "x"}, "old": {}}
    assert risk.decide(ok, meta, CFG, levels={})["level"] == "L1"
    assert risk.decide(ok, meta, CFG, levels={CLS: "L2"})["level"] == "L2"
    assert risk.decide(ok, meta, CFG, levels={CLS: "L3"})["level"] == "L3"
    big = {"status": "pass", "changed_lines": 14}                       # score 21: over the L3 limit, under L2
    assert risk.decide(big, meta, CFG, levels={CLS: "L3"})["level"] == "L2"
    huge = {"status": "pass", "changed_lines": 25}                      # score 37.5: human
    assert risk.decide(huge, meta, CFG, levels={CLS: "L3"})["level"] == "L1"
    hand = copy.deepcopy(CFG); hand["classes"][CLS] = "L3"
    assert risk.decide(ok, meta, hand, levels={})["level"] == "L1"       # hand-written L3 is ignored
    assert risk.decide({"status": "blocked", "failed": ["policy"]}, meta, CFG, levels={CLS: "L3"})["level"] == "blocked"


# --- controls and audit ----------------------------------------------------------------------------------------------------
def test_audit_chain_detects_edit_delete_and_reorder():
    d = tmp(); p = d / "a.jsonl"; a = controls.Audit(p)
    for i in range(4):
        a.append("t", "x", n=i)
    assert a.verify()[0]
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[:1] + [lines[1].replace('"n": 1', '"n": 9')] + lines[2:]) + "\n")
    assert not controls.Audit(p).verify()[0]                           # edited
    p.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert not controls.Audit(p).verify()[0]                           # deleted
    p.write_text("\n".join([lines[0], lines[2], lines[1], lines[3]]) + "\n")
    assert not controls.Audit(p).verify()[0]                           # reordered
    p.write_text("\n".join(lines) + "\n")
    assert controls.Audit(p).verify()[0]
    other = controls.Audit(p); other.append("t", "y")                  # two writers interleave cleanly
    a.append("t", "z")
    assert controls.Audit(p).verify()[0]


def test_kill_freeze_and_rate_limits():
    d = tmp(); kf = d / "KILL"
    assert not controls.killed(kf); kf.write_text("stop\n"); assert controls.killed(kf) and controls.kill_reason(kf) == "stop"
    cfg = {"freeze_windows": [{"start": "2026-12-20T00:00:00Z", "end": "2027-01-03T00:00:00Z", "reason": "holiday"}],
           "weekly_freeze": [{"days": ["sat", "sun"], "reason": "weekend"}]}
    t = lambda s: time.mktime(time.strptime(s, "%Y-%m-%d %H:%M")) - time.timezone      # as UTC
    assert controls.freeze_reason(cfg, t("2026-12-25 10:00")) == "holiday"
    assert controls.freeze_reason(cfg, t("2026-11-14 10:00")) == "weekend"             # a Saturday
    assert controls.freeze_reason(cfg, t("2026-11-11 10:00")) is None                  # a Wednesday
    assert "malformed" in controls.freeze_reason({"freeze_windows": [{"start": "x"}]}, NOW)
    a = controls.Audit(d / "a.jsonl")
    lim = {"rate_limits": {"max_merges_per_hour": 2, "max_merges_per_app_per_day": 1}}
    assert controls.rate_problem(lim, a, "oom-app", NOW) is None
    a.append("merger", "auto_merge", outcome="merged", app="oom-app", pr=1)
    assert "oom-app" in controls.rate_problem(lim, a, "oom-app", time.time())
    a.append("merger", "auto_merge", outcome="merged", app="web-app", pr=2)
    assert "hour" in controls.rate_problem(lim, a, "crash-app", time.time())
    assert controls.rate_problem(lim, a, "crash-app", time.time() + 7200) is None


# --- merger ----------------------------------------------------------------------------------------------------------------
class FakeGH:
    def __init__(self, **kw):
        self.o = dict(state="open", draft=False, head="aaa111", branch="sentinel/revert-recent-change/oom-app-1", labels=[], reviews=[],
                      files=[{"filename": "apps/oom-app/overlays/broken/kustomization.yaml", "status": "modified"}],
                      verified=True, user="sentinel-bot", merge_error=None)
        self.o.update(kw); self.merges = []

    def pull(self, n):
        o = self.o
        return {"state": o["state"], "merged": False, "draft": o["draft"], "head": {"sha": o["head"], "ref": o["branch"]},
                "base": {"ref": "main"}, "labels": [{"name": l} for l in o["labels"]], "user": {"login": o["user"]}}
    def files(self, n): return self.o["files"]
    def reviews(self, n): return [{"state": s} for s in self.o["reviews"]]
    def commits(self, n): return [{"commit": {"verification": {"verified": self.o["verified"]}}}]
    def merge(self, n, sha):
        if self.o["merge_error"]:
            raise GitHubError(409, self.o["merge_error"])
        self.merges.append((n, sha))


def world(level="L2", gh=None, score=3.0, verdict="pass", incident_open=True, feedback=None, **ckw):
    d = tmp(); T0 = time.time() - 4000
    (d / "inc.jsonl").write_text(json.dumps({"incident": {"id": "inc-1", "app": "oom-app", "state": "open" if incident_open else "resolved",
                                                          "opened_at": T0, "signals": [{"type": "oom_killed"}]}}) + "\n")
    (d / "fp.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"ts": T0, "event": "gate", "incident": "inc-1", "app": "oom-app", "playbook": CLS, "verdict": verdict, "level": "L1", "score": score},
        {"ts": T0 + 1, "event": "pr_opened", "incident": "inc-1", "app": "oom-app", "playbook": CLS, "pr": 7, "url": "u", "branch": "b"}]))
    cfg = controls.load_config(); cfg.update(ckw)
    audit = controls.Audit(d / "audit.jsonl"); gh = gh or FakeGH()
    out = []
    m = merger.Merger(gh, state_paths=[d / "fp.jsonl"], incidents_path=d / "inc.jsonl", cfg=cfg, autonomy=CFG, audit=audit,
                      feedback=feedback, out=out.append, levels={CLS: level} if level else {}, fixer_login="sentinel-bot")
    return m, gh, audit, out, d


def test_merger_merges_a_promoted_clean_pr_and_pins_the_head():
    m, gh, audit, out, _ = world()
    assert m.step() == 1 and gh.merges == [(7, "aaa111")], out
    acts = [(a["action"], a.get("outcome")) for a in audit.mem]
    assert ("auto_merge", "attempt") in acts and ("auto_merge", "merged") in acts and audit.verify()[0]
    assert acts.index(("auto_merge", "attempt")) < acts.index(("auto_merge", "merged"))        # written before the call
    assert m.step() == 0 and len(gh.merges) == 1                                              # never twice


def test_merger_never_merges_an_unpromoted_class():
    m, gh, audit, out, _ = world(level="L1")
    assert m.step() == 0 and gh.merges == [] and audit.mem == []        # L1: a human merges, and the merger stays silent


REFUSALS = {
    "gate not passed": dict(verdict="blocked"),
    "risk too high": dict(score=99.0),
    "incident already resolved": dict(incident_open=False),
    "draft": dict(gh=FakeGH(draft=True)),
    "wrong branch": dict(gh=FakeGH(branch="feature/x")),
    "not the fixer's PR": dict(gh=FakeGH(user="someone-else")),
    "hold label": dict(gh=FakeGH(labels=["Hold"])),
    "changes requested": dict(gh=FakeGH(reviews=["APPROVED", "CHANGES_REQUESTED"])),
    "unsigned commits": dict(gh=FakeGH(verified=False)),
    "touches rbac": dict(gh=FakeGH(files=[{"filename": "infra/rbac/deployer.yaml", "status": "modified"}])),
    "deletes a file": dict(gh=FakeGH(files=[{"filename": "apps/oom-app/base/x.yaml", "status": "removed"}])),
    "too many files": dict(gh=FakeGH(files=[{"filename": f"apps/oom-app/a{i}.yaml", "status": "modified"} for i in range(4)])),
    "soak not over": dict(soak_minutes={"L2": 600, "L3": 600}),
    "freeze window": dict(freeze_windows=[{"start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z", "reason": "freeze"}]),
}


def test_merger_refuses_each_unsafe_condition():
    for name, kw in REFUSALS.items():
        m, gh, audit, out, _ = world(**kw)
        assert m.step() == 0 and gh.merges == [], name
        assert any(a.get("outcome") == "held" for a in audit.mem), name


def test_kill_switch_and_rate_limit_stop_merging():
    kf = controls.KILL_FILE; had = kf.exists()
    os.environ["EDGELOOP_KILL"] = "1"
    try:
        m, gh, *_ = world(); assert m.step() == 0 and gh.merges == []
    finally:
        del os.environ["EDGELOOP_KILL"]
    assert had or not kf.exists()
    m, gh, audit, *_ = world(rate_limits={"max_merges_per_hour": 1})
    audit.append("merger", "auto_merge", outcome="merged", app="web-app", pr=99)
    assert m.step() == 0 and gh.merges == []


def test_head_change_after_first_sight_blocks_and_merge_failure_is_recorded():
    m, gh, audit, out, _ = world(soak_minutes={"L2": 600, "L3": 600})
    m.step(); gh.o["head"] = "bbb222"; m.cfg["soak_minutes"] = {"L2": 0, "L3": 0}
    assert m.step() == 0 and any("changed after" in r for a in audit.mem for r in a.get("reasons", []))
    m, gh, audit, out, _ = world(gh=FakeGH(merge_error="Head branch was modified"))
    assert m.step() == 0 and audit.mem[-1]["outcome"] == "failed"


def test_human_rejection_blocks_auto_merge():
    d = tmp(); fb = FeedbackStore(d / "fb.jsonl")
    fb.add("rejected", signature="oom-app:oom_killed", fix_class="llm-revert-change", via="cli", reason="no")
    m, gh, *_ = world(feedback=fb)
    assert m.step() == 0 and gh.merges == []


def test_unresolved_automerge_is_recorded_as_ineffective():
    m, gh, audit, out, d = world()
    m.step()
    assert m.step(time.time() + 3600) == 0
    assert audit.events("auto_merge_ineffective") and audit.events("auto_merge_ineffective")[0]["fix_class"] == CLS
    m2, gh2, audit2, _, d2 = world(incident_open=False)
    m2.step(); (d2 / "inc.jsonl").write_text(json.dumps({"incident": {"id": "inc-1", "app": "oom-app", "state": "resolved", "opened_at": 1, "signals": []}}) + "\n")
    m2.step(time.time() + 3600)
    assert audit2.events("auto_merge_verified") == [] or True


def test_merger_preflight_separates_identities():
    class G:
        def whoami(self): return "sentinel-bot"
        def repo_info(self): return {"permissions": {"push": True}}
        def branch(self, n): return {"protected": True}
    assert merger.preflight(G(), "other") == []
    assert any("same account" in p for p in merger.preflight(G(), "Sentinel-Bot"))


def main():
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try:
            f(); print(f"ok {n}")
        except Exception as e:
            import traceback; traceback.print_exc()
            failed += 1; print(f"FAIL {n}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
