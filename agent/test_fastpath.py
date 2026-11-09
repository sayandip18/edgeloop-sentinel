"""Offline tests for the Phase 4 fast path. No cluster, no network, no token.
Run: python agent/test_fastpath.py   (also collectable by pytest). Needs git and kubectl (kustomize) on PATH.

Builds a throwaway git repo from the real apps/ tree, injects each Phase 2 scenario as a commit, and checks that the
revert playbook produces a plan that restores the healthy overlay and passes the kustomize build gate."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
import yaml  # noqa: E402
import playbooks  # noqa: E402
from fastpath import FastPath, State, md_code, render_diff, validate_plan  # noqa: E402
from github import GitHub, GitHubError, preflight  # noqa: E402
from gitrepo import GitRepo  # noqa: E402
from playbooks import raise_memory_limit  # noqa: E402

T_BASE = 1_700_000_000          # baseline commit time
T_INJECT = T_BASE + 7200        # change lands 2h later
T_INC = T_INJECT + 60           # incident opens a minute after the change


def git(cwd, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{int(when)} +0000"
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def make_repo():
    d = Path(tempfile.mkdtemp(prefix="sentinel-test-"))
    shutil.copytree(ROOT / "apps", d / "apps", ignore=shutil.ignore_patterns("app"))
    (d / "infra" / "flux").mkdir(parents=True)
    shutil.copy(ROOT / "infra" / "flux" / "kustomizations.yaml", d / "infra" / "flux" / "kustomizations.yaml")
    git(d, "init", "-q", "-b", "main")
    git(d, "add", "-A"); git(d, "commit", "-q", "-m", "baseline", when=T_BASE)
    return d


def commit(d, subject, when):
    git(d, "add", "-A"); git(d, "commit", "-q", "-m", subject, when=when)
    return git(d, "rev-parse", "HEAD")


def incident(app, signals=("repeated_restarts",), opened=T_INC, iid="inc-0001-x"):
    return {"id": iid, "app": app, "affected_apps": [], "state": "open", "opened_at": opened, "last_seen": opened, "resolved_at": None,
            "signals": [{"type": t, "app": app, "pod": f"{app}-1", "detail": f"{t} detail", "first_seen": opened, "last_seen": opened, "count": 1}
                        for t in signals]}


CFG = playbooks.Config()
SCN = {f.stem: yaml.safe_load(f.read_text(encoding="utf-8")) for f in (ROOT / "evals" / "scenarios").glob("*.yaml")}


# --- revert playbook ------------------------------------------------------------------
def test_revert_plan_for_every_fix_scenario():
    d = make_repo(); base = git(d, "rev-parse", "HEAD")
    try:
        repo = GitRepo(d, "main")
        n = 0
        for sid, s in sorted(SCN.items()):
            if s["expected"]["action"] != "fix":
                continue
            app = s["inject"]["app"]
            healthy = d / "apps" / app / "overlays" / "healthy" / "kustomization.yaml"
            original = healthy.read_text(encoding="utf-8")
            shutil.copyfile(d / "apps" / app / "overlays" / s["inject"]["overlay"] / "kustomization.yaml", healthy)
            commit(d, f"eval: inject {sid} (will be reverted)", T_INJECT)
            plan, declines = playbooks.select(incident(s["expected"]["diagnosis"]["target_app"]), repo, CFG)
            assert plan, f"{sid}: no plan: {declines}"
            assert plan.playbook == "revert-recent-change", sid
            assert plan.app == app and list(plan.files) == [f"apps/{app}/overlays/healthy/kustomization.yaml"], (sid, plan.files)
            assert plan.files[f"apps/{app}/overlays/healthy/kustomization.yaml"] == original, f"{sid}: not an exact undo"
            err = validate_plan(plan, repo)
            assert err is None, f"{sid}: {err}"
            git(d, "reset", "-q", "--hard", base); n += 1
        assert n == 7, n
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_revert_declines():
    d = make_repo(); base = git(d, "rev-parse", "HEAD")
    try:
        repo = GitRepo(d, "main")
        ov = d / "apps" / "web-app" / "overlays" / "healthy" / "kustomization.yaml"
        bad = (d / "apps" / "web-app" / "overlays" / "wrong-env-var" / "kustomization.yaml").read_text(encoding="utf-8")
        inc = incident("web-app")

        def declined(why):
            plan, declines = playbooks.select(inc, repo, CFG)
            assert plan is None, f"expected decline ({why})"
            return " ".join(r for _, r in declines)

        # no change at all within the window: the only commit is the 2h-old baseline... make it older than lookback
        assert "older than the incident" in declined("stale"), "stale"
        # a revert commit is never reverted
        ov.write_text(bad, encoding="utf-8"); commit(d, 'Revert "eval: inject x"', T_INJECT)
        assert "revert/fix" in declined("revert"); git(d, "reset", "-q", "--hard", base)
        # our own earlier fix is never reverted
        ov.write_text(bad, encoding="utf-8"); commit(d, "sentinel(web-app): revert abc1234", T_INJECT)
        assert "revert/fix" in declined("sentinel"); git(d, "reset", "-q", "--hard", base)
        # change touching another directory
        ov.write_text(bad, encoding="utf-8"); (d / "infra" / "flux" / "kustomizations.yaml").write_text("# x\n", encoding="utf-8")
        shutil.copy(ROOT / "infra" / "flux" / "kustomizations.yaml", d / "infra" / "flux" / "kustomizations.yaml")
        (d / "infra" / "flux" / "extra.yaml").write_text("a: 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(d), "mv", "-f", "apps/web-app/overlays/healthy/kustomization.yaml", "apps/web-app/overlays/healthy/k2.yaml"], capture_output=True)
        commit(d, "mixed", T_INJECT)
        assert "will not revert" in declined("mixed"); git(d, "reset", "-q", "--hard", base)
        # a later change to the same file: not an exact undo
        ov.write_text(bad, encoding="utf-8"); commit(d, "inject", T_INJECT)
        ov.write_text(bad + "# later\n", encoding="utf-8"); commit(d, "tweak", T_INC + 600)
        assert "changed again" in declined("later"); git(d, "reset", "-q", "--hard", base)
        # a commit that lands long after the incident opened is not the cause (and is not picked)
        ov.write_text(bad, encoding="utf-8"); commit(d, "late", T_INC + 3600)
        assert declined("after")
        git(d, "reset", "-q", "--hard", base)
        # unsafe / unknown app names never reach a path
        for name in ("../infra", "Web App", "nope-app", ""):
            plan, _ = playbooks.select(incident(name), repo, CFG)
            assert plan is None, name
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- memory playbook ------------------------------------------------------------------
def test_memory_bump():
    flow = "resources:\n  limits: {cpu: 100m, memory: 128Mi}\n"
    new, old, n = raise_memory_limit.bump(flow, CFG)
    assert (old, n) == (128, 192) and "memory: 192Mi" in new and "cpu: 100m" in new
    assert raise_memory_limit.bump(flow.replace("128Mi", "200Mi"), CFG)[2] == 320
    assert raise_memory_limit.bump(flow.replace("128Mi", "400Mi"), CFG)[2] == 512        # capped
    block = "resources:\n  requests:\n    memory: 16Mi\n  limits:\n    cpu: 100m\n    memory: 1Gi\n"
    for bad_text, why in ((block, "at cap"), ("x: 1\n", "none"), (flow + flow, "two")):
        try:
            raise_memory_limit.bump(bad_text, CFG); assert False, why
        except ValueError:
            pass
    blk = block.replace("1Gi", "256Mi")
    new, old, n = raise_memory_limit.bump(blk, CFG)
    assert (old, n) == (256, 384) and "memory: 16Mi" in new          # the request is left alone


def test_memory_fallback_when_no_recent_change():
    d = make_repo()
    try:
        repo = GitRepo(d, "main")
        # only the baseline commit exists and it is 2h older than the incident, outside a 1h window
        inc = incident("oom-app", ("oom_killed", "crash_loop"), opened=T_BASE + 7200)
        plan, declines = playbooks.select(inc, repo, CFG)
        assert plan and plan.playbook == "raise-memory-limit", declines
        assert plan.expect == {"memory_limit_mi": 192}
        assert validate_plan(plan, repo) is None
        assert "128Mi" in render_diff(plan, repo) and "192Mi" in render_diff(plan, repo)
        # without an OOM signal the memory playbook stays out of it
        plan, declines = playbooks.select(incident("oom-app", ("crash_loop",), opened=T_BASE + 7200), repo, CFG)
        assert plan is None
        # a plan that would not build is rejected by the gate
        broken = playbooks.Plan("x", "oom-app", {"apps/oom-app/overlays/healthy/kustomization.yaml": "resources: [../../nope]\n"}, "t", "r")
        assert "kustomize build failed" in (validate_plan(broken, repo) or "")
        outside = playbooks.Plan("x", "oom-app", {"infra/flux/kustomizations.yaml": ""}, "t", "r")
        assert "outside" in (validate_plan(outside, repo) or "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- GitHub client + preflight (fake transport) ----------------------------------------------
class FakeGH:
    """Records every call; answers like the GitHub API for the handful of endpoints the fixer uses."""
    def __init__(self, *, admin=False, push=True, protected=True, token_ok=True, merged=False, pr_state="open"):
        self.calls, self.admin, self.push, self.protected, self.token_ok = [], admin, push, protected, token_ok
        self.merged, self.pr_state = merged, pr_state

    def __call__(self, method, url, headers, body):
        path = url.split("api.github.com", 1)[1]
        self.calls.append((method, path, json.loads(body) if body else None))
        if not self.token_ok:
            return 401, {"message": "Bad credentials"}
        if path == "/user": return 200, {"login": "sentinel-bot"}
        if path == "/repos/o/r": return 200, {"permissions": {"admin": self.admin, "push": self.push, "pull": True}}
        if path == "/repos/o/r/branches/main": return 200, {"protected": self.protected}
        if path.startswith("/repos/o/r/git/commits/") and method == "GET": return 200, {"tree": {"sha": "tree0"}}
        if path == "/repos/o/r/git/trees": return 201, {"sha": "tree1"}
        if path == "/repos/o/r/git/commits": return 201, {"sha": "commit1"}
        if path == "/repos/o/r/git/refs": return 201, {}
        if path == "/repos/o/r/pulls": return 201, {"number": 7, "html_url": "https://github.com/o/r/pull/7"}
        if path == "/repos/o/r/pulls/7": return 200, {"state": self.pr_state, "merged": self.merged}
        if method == "DELETE": return 204, None
        return 404, {"message": f"unexpected {method} {path}"}

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


def test_preflight():
    ok = lambda **kw: preflight(GitHub("o/r", "t", FakeGH(**kw)))
    assert ok() == []
    assert any("not protected" in p for p in ok(protected=False))
    assert any("ADMIN" in p for p in ok(admin=True))
    assert preflight(GitHub("o/r", "t", FakeGH(admin=True)), allow_admin=True) == []
    assert any("no write access" in p for p in ok(push=False))
    assert any("rejected" in p for p in ok(token_ok=False))


def test_open_fix_pr_only_creates_new_branch_and_never_merges():
    t = FakeGH(); gh = GitHub("o/r", "t", t)
    n, url = gh.open_fix_pr(base_branch="main", base_sha="abc", branch="sentinel/x/app-1", files={"a/b.yaml": "x: 1\n"},
                            message="m", title="T", body="B")
    assert n == 7 and url.endswith("/pull/7")
    refs = [c for c in t.calls if c[1] == "/repos/o/r/git/refs"]
    assert refs and refs[0][2]["ref"] == "refs/heads/sentinel/x/app-1"
    commit_call = [c for c in t.calls if c[1] == "/repos/o/r/git/commits" and c[0] == "POST"][0]
    assert commit_call[2]["parents"] == ["abc"]
    assert not any("merge" in p or p.endswith("/heads/main") for _, p in t.writes()), t.writes()
    for bad in ("main", "feature/x"):
        try:
            gh.open_fix_pr(base_branch="main", base_sha="abc", branch=bad, files={"a": "b"}, message="m", title="T", body="B")
            assert False, bad
        except ValueError:
            pass
    try:
        gh.delete_own_branch("main"); assert False
    except ValueError:
        pass


def test_pr_body_neutralises_untrusted_text():
    s = md_code("evil `@everyone` [x](http://e)\nnewline")
    assert s.startswith("`") and s.endswith("`") and "\n" not in s and s.count("`") == 2


# --- the loop, end to end with fakes ---------------------------------------------------------------
class NoFetchRepo(GitRepo):
    def fetch(self, url, branch="main"):
        pass


def write_incidents(path, incs, ts=None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 0, "event": "watch_started"}) + "\n")
        for i in incs:
            f.write(json.dumps({"ts": ts or i["opened_at"], "event": "opened", "incident": i}) + "\n")


def test_loop_opens_one_pr_per_incident_and_none_for_nothing():
    d = make_repo(); tmp = Path(tempfile.mkdtemp(prefix="sentinel-loop-"))
    try:
        app = "oom-app"
        healthy = d / "apps" / app / "overlays" / "healthy" / "kustomization.yaml"
        shutil.copyfile(d / "apps" / app / "overlays" / "broken" / "kustomization.yaml", healthy)
        commit(d, "eval: inject oom-kill", T_INJECT)
        log, out = tmp / "inc.jsonl", []
        t = FakeGH(); st = State(tmp / "fp.jsonl")
        fp = FastPath(NoFetchRepo(d, "main"), st, GitHub("o/r", "t", t), incidents_path=log, fetch_url="x", settle_s=30, out=out.append)

        write_incidents(log, [])                                   # decoy case: no incident, no PR
        assert fp.step(now=T_INC + 100) == 0 and not t.writes() and not st.prs()

        write_incidents(log, [incident(app, ("oom_killed",), iid="inc-0001-oom-app")])
        assert fp.step(now=T_INC + 5) == 0 and not t.writes()      # still inside the settle window
        assert fp.step(now=T_INC + 40) == 1
        prs = st.prs()
        assert len(prs) == 1 and prs[0]["app"] == app and prs[0]["playbook"] == "revert-recent-change" and prs[0]["pr"] == 7
        pr_post = [c for c in t.calls if c[1] == "/repos/o/r/pulls" and c[0] == "POST"][0][2]
        assert pr_post["base"] == "main" and pr_post["head"].startswith("sentinel/revert-recent-change/oom-app-")
        assert "L1" in pr_post["body"] and "```diff" in pr_post["body"] and "Rollback" in pr_post["body"]
        assert all("merge" not in p for _, p in t.writes())
        n_writes = len(t.writes())
        assert fp.step(now=T_INC + 80) == 0 and len(t.writes()) == n_writes     # same incident: no second PR

        # a restart of the process (new State object on the same file) still remembers it
        fp2 = FastPath(NoFetchRepo(d, "main"), State(tmp / "fp.jsonl"), GitHub("o/r", "t", t), incidents_path=log, fetch_url="x", out=out.append)
        assert fp2.step(now=T_INC + 80) == 0 and len(t.writes()) == n_writes

        # budget: while the PR is open, a NEW incident on the same app is held back and escalated
        write_incidents(log, [incident(app, ("oom_killed",), iid="inc-0001-oom-app"), incident(app, ("oom_killed",), iid="inc-0002-oom-app")])
        assert fp.step(now=T_INC + 200) == 1
        assert any("still open" in o for o in out) and len(t.writes()) == n_writes

        # merged PR gets recorded and its branch deleted
        t.pr_state, t.merged = "closed", True
        fp.step(now=T_INC + 300)
        assert [p["status"] for p in st.prs()] == ["merged"]
        assert any(m == "DELETE" for m, _ in t.writes())
    finally:
        shutil.rmtree(d, ignore_errors=True); shutil.rmtree(tmp, ignore_errors=True)


def test_loop_declines_unknown_failure_and_dry_run_writes_nothing():
    d = make_repo(); tmp = Path(tempfile.mkdtemp(prefix="sentinel-loop-"))
    try:
        log, out = tmp / "inc.jsonl", []
        # failure on an app with no recent change and no OOM: nothing deterministic applies -> escalate, no PR
        write_incidents(log, [incident("crash-app", ("crash_loop",), opened=T_BASE + 7200)])
        t = FakeGH(); st = State(tmp / "fp.jsonl")
        fp = FastPath(NoFetchRepo(d, "main"), st, GitHub("o/r", "t", t), incidents_path=log, fetch_url="x", out=out.append)
        fp.step(now=T_BASE + 7300)
        assert not t.writes() and any("escalate" in o for o in out)
        assert [e["event"] for e in st.mem if e["event"] != "started"] == ["declined"]
        fp.step(now=T_BASE + 7400)
        assert [e["event"] for e in st.mem].count("declined") == 1          # escalated once, not every pass

        # dry run on a fixable incident: prints a diff, no GitHub client at all, nothing persisted
        shutil.copyfile(d / "apps/oom-app/overlays/broken/kustomization.yaml", d / "apps/oom-app/overlays/healthy/kustomization.yaml")
        commit(d, "eval: inject oom-kill", T_INJECT)
        write_incidents(log, [incident("oom-app", ("oom_killed",), iid="inc-0009-oom-app")])
        out.clear(); st2 = State(None)
        FastPath(NoFetchRepo(d, "main"), st2, None, incidents_path=log, fetch_url="x", dry_run=True, out=out.append).step(now=T_INC + 60)
        assert any("dry-run" in o and "-          value:" not in o and "value" in o for o in out) and not st2.mem
    finally:
        shutil.rmtree(d, ignore_errors=True); shutil.rmtree(tmp, ignore_errors=True)


def _gate_world(gate_fn):
    d = make_repo(); tmp = Path(tempfile.mkdtemp(prefix="sentinel-gate-loop-"))
    app = "oom-app"
    shutil.copyfile(d / "apps" / app / "overlays" / "broken" / "kustomization.yaml", d / "apps" / app / "overlays" / "healthy" / "kustomization.yaml")
    commit(d, "eval: inject oom-kill", T_INJECT)
    log, out = tmp / "inc.jsonl", []
    write_incidents(log, [incident(app, ("oom_killed",), iid="inc-0001-oom-app")])
    t = FakeGH(); st = State(tmp / "fp.jsonl")
    fp = FastPath(NoFetchRepo(d, "main"), st, GitHub("o/r", "t", t), incidents_path=log, fetch_url="x", settle_s=30, out=out.append, gate_fn=gate_fn)
    return d, tmp, fp, t, st, out


def test_gate_block_means_no_pr():
    for verdict in ({"status": "blocked", "failed": ["policy"], "skipped": [], "apps": ["oom-app"], "changed_lines": 2, "checks": []},
                    {"status": "incomplete", "failed": [], "skipped": ["schema"], "apps": ["oom-app"], "changed_lines": 2, "checks": []}):
        d, tmp, fp, t, st, out = _gate_world(lambda plan, v=verdict: v)
        try:
            fp.step(now=T_INC + 40)
            assert not t.writes() and not st.prs(), "a blocked/incomplete gate must not open a PR"
            assert any(e["event"] == "declined" for e in st.mem) and any("escalate" in o for o in out)
            assert any(e["event"] == "gate" and e["level"] == "blocked" for e in st.mem)
        finally:
            shutil.rmtree(d, ignore_errors=True); shutil.rmtree(tmp, ignore_errors=True)


def test_gate_pass_puts_the_report_in_the_pr():
    ok = {"status": "pass", "failed": [], "skipped": [], "apps": ["oom-app"], "changed_lines": 2,
          "checks": [{"name": "scope", "status": "pass", "detail": ["1 file"]}]}
    d, tmp, fp, t, st, out = _gate_world(lambda plan: ok)
    try:
        fp.step(now=T_INC + 40)
        body = [c for c in t.calls if c[1] == "/repos/o/r/pulls" and c[0] == "POST"][0][2]["body"]
        assert "gate verdict: **pass**" in body and "autonomy decision: **L1**" in body
    finally:
        shutil.rmtree(d, ignore_errors=True); shutil.rmtree(tmp, ignore_errors=True)


def test_real_gate_runs_in_the_loop():
    d, tmp, fp, t, st, out = _gate_world(None)       # default gate_fn: the real policy/gate.py in its own process
    try:
        fp.step(now=T_INC + 40)
        g = [e for e in st.mem if e["event"] == "gate"]
        assert g and g[0]["verdict"] in ("pass", "incomplete"), g
        assert len(st.prs()) == (1 if g[0]["level"] == "L1" else 0)
    finally:
        shutil.rmtree(d, ignore_errors=True); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
