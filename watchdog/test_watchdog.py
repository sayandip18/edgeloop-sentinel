"""Offline tests for the rollback watchdog: a local bare repo as 'origin', fake health, fake clock. No cluster.
Run: python watchdog/test_watchdog.py"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import watchdog as wd  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def g(cwd, *a):
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, f"git {a}: {r.stderr}"
    return r.stdout.strip()


class Clock:
    t = 1000.0
    def __call__(self): return self.t


def world():
    d = Path(tempfile.mkdtemp(prefix="sentinel-wdtest-"))
    bare, work, mon = d / "origin.git", d / "work", d / "monitor"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    g(work, "checkout", "-q", "-b", "main")
    (work / "apps").mkdir(); (work / "apps" / "f.txt").write_text("v1\n")
    g(work, "add", "-A"); g(work, "commit", "-q", "-m", "baseline"); g(work, "push", "-q", "origin", "main")
    subprocess.run(["git", "init", "-q", str(mon)], check=True)
    return d, bare, work, mon


def land(work, content, subject):
    (work / "apps" / "f.txt").write_text(content)
    g(work, "add", "-A"); g(work, "commit", "-q", "-m", subject); g(work, "push", "-q", "origin", "main")
    return g(work, "rev-parse", "HEAD")


def make(mon, bare, health, clock, **kw):
    log = wd.Log(None, out=lambda s: None)
    w = wd.Watchdog(mon, str(bare), log, health, clock=clock, **{"timeout": 100, "min_observe": 20, "stable": 10, "fail_after": 10, **kw})
    w.step()    # first start: sets the cursor, judges nothing
    return w, log


def run_until(w, clock, secs, step=5):
    for _ in range(int(secs / step)):
        clock.t += step; w.step()


def events(log): return [e["event"] for e in log.events]


def test_unhealthy_change_is_reverted():
    d, bare, work, mon = world()
    try:
        clock, st = Clock(), {"ok": True}
        w, log = make(mon, bare, lambda app: (st["ok"], "x", {"d": 1}), clock)
        land(work, "bad\n", "sentinel(web-app): bad patch")
        st["ok"] = False
        run_until(w, clock, 60)
        assert "reverted" in events(log), log.events
        g(work, "pull", "-q", "origin", "main")
        assert (work / "apps" / "f.txt").read_text() == "v1\n", "file not restored"
        assert g(work, "log", "-1", "--format=%s").startswith("Revert"), "revert subject must start with Revert"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_healthy_change_is_confirmed_and_left_alone():
    d, bare, work, mon = world()
    try:
        clock = Clock()
        w, log = make(mon, bare, lambda app: (True, "ok", {"d": 1}), clock)
        land(work, "good\n", "sentinel(web-app): good patch")
        run_until(w, clock, 60)
        assert "confirmed" in events(log) and "reverted" not in events(log), log.events
        g(work, "pull", "-q", "origin", "main")
        assert (work / "apps" / "f.txt").read_text() == "good\n"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_silence_reverts_dead_mans_switch():
    d, bare, work, mon = world()
    try:
        clock = Clock()
        w, log = make(mon, bare, lambda app: (False, "cluster unreachable", {}), clock)
        land(work, "mystery\n", "sentinel(oom-app): patch")
        run_until(w, clock, 150)
        assert "reverted" in events(log), log.events
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_human_and_injected_commits_are_never_touched():
    d, bare, work, mon = world()
    try:
        clock = Clock()
        w, log = make(mon, bare, lambda app: (False, "broken", {}), clock)
        land(work, "inj\n", "eval: inject oom-kill")
        land(work, "human\n", "fix: something by a person")
        run_until(w, clock, 150)
        assert "tracking" not in events(log) and "reverted" not in events(log), log.events
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dry_run_and_kill_switch_do_not_push():
    d, bare, work, mon = world()
    try:
        for kw in ({"dry_run": True}, {"kill_switch": str(d / "DISABLE")}):
            (d / "DISABLE").write_text("x")
            clock = Clock()
            w, log = make(mon, bare, lambda app: (False, "bad", {}), clock, **kw)
            sha = land(work, "bad2\n", "sentinel(web-app): p")
            run_until(w, clock, 150)
            assert "reverted" not in events(log) and ("would_revert" in events(log) or "revert_blocked" in events(log)), log.events
            assert g(bare, "rev-parse", "main") == sha, "origin/main moved"
            g(work, "reset", "-q", "--hard", "HEAD~1"); g(work, "push", "-q", "-f", "origin", "main")
            mon2 = d / "monitor"; shutil.rmtree(mon2, ignore_errors=True); subprocess.run(["git", "init", "-q", str(mon2)], check=True)
            mon = mon2
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_conflicting_revert_is_not_forced():
    d, bare, work, mon = world()
    try:
        clock, st = Clock(), {"ok": True}
        w, log = make(mon, bare, lambda app: (st["ok"], "x", {}), clock)
        land(work, "bad\n", "sentinel(web-app): bad patch")
        land(work, "later human edit\n", "human: edit after")
        st["ok"] = False
        run_until(w, clock, 60)
        assert "revert_failed" in events(log) and "reverted" not in events(log), log.events
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_tracker_new_generation_marks_applied_early():
    t = wd.Tracker("s", "a", 0, {"d": 1}, timeout=300, min_observe=90, stable=10, fail_after=10)
    assert t.poll(5, True, "ok", {"d": 1}) is None and t.applied_at is None   # old healthy state must not confirm
    assert t.poll(10, True, "ok", {"d": 2}) is None and t.applied_at == 10
    assert t.poll(21, True, "ok", {"d": 2})[0] == "confirmed"


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in tests:
        try:
            f(); print(f"ok {n}")
        except Exception as e:
            bad += 1; print(f"FAIL {n}: {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
