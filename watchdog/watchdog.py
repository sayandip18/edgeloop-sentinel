#!/usr/bin/env python3
"""Rollback watchdog (Phase 5): a host process, outside Kubernetes, that reverts a sentinel change that does not
prove healthy in time (a dead-man's switch).

    python watchdog/watchdog.py run [--once] [--dry-run] [--timeout 300] [--interval 15] [--kubeconfig PATH]
                                    [--remote-url URL] [--state PATH] [--kill-switch PATH]

What it watches: commits landing on origin/main whose subject starts with `sentinel(` (what the fast path's PRs
are titled; GitHub keeps that on merge or squash). Human commits and the eval harness's injected failures are
never touched.

For each such commit it tracks the changed app:
  * "applied" once a Deployment of the app shows a new generation, or after --min-observe seconds (Flux needs up to
    a minute plus rollout), whichever comes first;
  * CONFIRMED once the app is healthy continuously for --stable seconds after that;
  * REVERTED when the app is unhealthy for --fail-after seconds after it was applied, or when it is still not
    confirmed at --timeout (a dead-man's switch: silence means rollback; an unreachable API counts as unhealthy).

Revert = `git revert` in a throwaway clone, pushed to main with THE HOST'S git credentials (this process's own
identity, never the fixer's PAT: the fixer cannot write main, by design). Subject starts with "Revert", so the
playbooks never revert a revert. If the revert conflicts it does nothing and logs `revert_failed` (a human decides).
Health is read with the read-only diagnoser kubeconfig. Pass --dry-run to log what it would revert.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"^sentinel\(([a-z0-9][a-z0-9-]*)\)")
NS = "apps"


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


class Log:
    def __init__(self, path, out=print):
        self.path, self.out, self.events = Path(path) if path else None, out, []
        if self.path and self.path.exists():
            self.events = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def add(self, event, **kw):
        ev = {"ts": time.time(), "event": event, **kw}
        self.events.append(ev)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        self.out(f"{event:<14} {kw.get('sha', '')[:8]} {kw.get('app', '')} {kw.get('reason', '')}".rstrip())
        return ev

    def decided(self, sha):
        return any(e.get("sha") == sha and e["event"] in ("confirmed", "reverted", "revert_failed", "would_revert") for e in self.events)

    def last_seen(self):
        for e in reversed(self.events):
            if e["event"] == "cursor":
                return e["sha"]
        return None


# --- health (read-only) --------------------------------------------------------------------------------------------
def kubectl_health(kubeconfig, context=None):
    """health(app) -> (ok, detail, generations). Uses only get verbs the diagnoser already has."""
    def health(app):
        base = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + (["--context", context] if context else [])
        r = subprocess.run(base + ["-n", NS, "get", "deployments,pods", "-l", f"app={app}", "-o", "json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            return False, f"cluster unreachable: {r.stderr.strip()[:120]}", {}
        items = json.loads(r.stdout or "{}").get("items", [])
        deps = [i for i in items if i["kind"] == "Deployment"]
        pods = [i for i in items if i["kind"] == "Pod"]
        gens = {d["metadata"]["name"]: d["metadata"].get("generation") for d in deps}
        if not deps:
            return False, "no deployment found", gens
        for d in deps:
            s, want = d.get("status", {}), d["spec"].get("replicas", 1)
            if s.get("observedGeneration") != d["metadata"].get("generation") or s.get("availableReplicas", 0) < want or s.get("updatedReplicas", 0) < want:
                return False, f"{d['metadata']['name']} not fully rolled out", gens
        for p in pods:
            cs = p.get("status", {}).get("containerStatuses") or []
            if not cs or any((not c.get("ready")) for c in cs):
                return False, f"pod {p['metadata']['name']} not ready", gens
            for c in cs:
                w = (c.get("state") or {}).get("waiting")
                if w and w.get("reason") in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"):
                    return False, f"pod {p['metadata']['name']}: {w['reason']}", gens
        return True, "all pods ready", gens
    return health


# --- per-change decision (pure; clock and health injected) ------------------------------------------------------------
class Tracker:
    def __init__(self, sha, app, start, gens, *, timeout, min_observe, stable, fail_after):
        self.sha, self.app, self.start, self.gens0 = sha, app, start, gens
        self.timeout, self.min_observe, self.stable, self.fail_after = timeout, min_observe, stable, fail_after
        self.applied_at = None
        self.healthy_since = None
        self.unhealthy_since = None

    def poll(self, now, ok, detail, gens):
        """Returns None (keep watching), ('confirmed', why) or ('revert', why)."""
        if self.applied_at is None and ((gens and gens != self.gens0) or now - self.start >= self.min_observe):
            self.applied_at = now
        if ok:
            self.unhealthy_since = None
            if self.applied_at is not None:
                self.healthy_since = self.healthy_since or now
                if now - self.healthy_since >= self.stable:
                    return "confirmed", f"healthy for {self.stable:.0f}s after applying"
        else:
            self.healthy_since = None
            if self.applied_at is not None:
                self.unhealthy_since = self.unhealthy_since or now
                if now - self.unhealthy_since >= self.fail_after:
                    return "revert", f"unhealthy for {self.fail_after:.0f}s after applying: {detail}"
        if now - self.start >= self.timeout:
            return "revert", f"not confirmed healthy within {self.timeout:.0f}s ({detail})"
        return None


# --- git ------------------------------------------------------------------------------------------------------------------
def new_sentinel_commits(repo, ref, since):
    """Oldest-first [(sha, app, subject, ts)] of sentinel(...) commits on `ref` after `since` (all of history if None)."""
    rng = f"{since}..{ref}" if since else ref
    out = git(repo, "log", "--reverse", "-n", "50", "--format=%H%x1f%ct%x1f%s", rng).stdout
    res = []
    for line in out.splitlines():
        sha, ts, subj = line.split("\x1f", 2)
        m = MARKER.match(subj)
        if m:
            res.append((sha, m.group(1), subj, float(ts)))
    return res


def do_revert(remote_url, sha, branch="main", why=""):
    """Revert `sha` on top of the remote's branch in a throwaway clone and push. Returns (ok, message)."""
    d = tempfile.mkdtemp(prefix="sentinel-wd-")
    try:
        subprocess.run(["git", "clone", "-q", "--branch", branch, remote_url, d + "/r"], check=True, capture_output=True)
        r = d + "/r"
        parents = git(r, "rev-list", "--parents", "-n", "1", sha).stdout.split()
        args = ["revert", "--no-edit"] + (["-m", "1"] if len(parents) > 2 else []) + [sha]
        res = git(r, *args, check=False)
        if res.returncode != 0:
            git(r, "revert", "--abort", check=False)
            return False, f"revert did not apply cleanly (later changes?): {res.stderr.strip()[:200]}"
        git(r, "commit", "--amend", "-q", "-m",
            git(r, "log", "-1", "--format=%B").stdout.strip() + f"\n\nsentinel-watchdog: {why[:200]}")
        p = git(r, "push", "-q", "origin", f"HEAD:{branch}", check=False)
        return (p.returncode == 0), ("pushed" if p.returncode == 0 else f"push failed: {p.stderr.strip()[:200]}")
    except (subprocess.CalledProcessError, RuntimeError) as e:
        return False, str(e)[:300]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- the loop -----------------------------------------------------------------------------------------------------------------
class Watchdog:
    def __init__(self, repo, remote_url, log, health, *, timeout=300, min_observe=90, stable=45, fail_after=30,
                 dry_run=False, kill_switch=None, clock=time.time, ref="refs/sentinel/wd"):
        self.repo, self.remote, self.log, self.health = repo, remote_url, log, health
        self.cfg = dict(timeout=timeout, min_observe=min_observe, stable=stable, fail_after=fail_after)
        self.dry_run, self.kill_switch, self.clock, self.ref = dry_run, kill_switch, clock, ref
        self.tracking = {}

    def discover(self):
        git(self.repo, "fetch", "-q", self.remote, f"+refs/heads/main:{self.ref}")
        head = git(self.repo, "rev-parse", self.ref).stdout.strip()
        since = self.log.last_seen()
        if since is None:      # first start: only watch from now on, never judge old history
            self.log.add("cursor", sha=head, reason="first start")
            return
        for sha, app, subj, ts in new_sentinel_commits(self.repo, self.ref, since):
            if sha not in self.tracking and not self.log.decided(sha):
                gens = self.health(app)[2]
                self.tracking[sha] = Tracker(sha, app, self.clock(), gens, **self.cfg)
                self.log.add("tracking", sha=sha, app=app, reason=subj[:80])
        if head != since:
            self.log.add("cursor", sha=head)

    def step(self):
        self.discover()
        for sha, t in list(self.tracking.items()):
            ok, detail, gens = self.health(t.app)
            r = t.poll(self.clock(), ok, detail, gens)
            if not r:
                continue
            kind, why = r
            del self.tracking[sha]
            if kind == "confirmed":
                self.log.add("confirmed", sha=sha, app=t.app, reason=why)
            elif self.kill_switch and Path(self.kill_switch).exists():
                self.log.add("revert_blocked", sha=sha, app=t.app, reason=f"kill switch {self.kill_switch} present; wanted to revert: {why}")
            elif self.dry_run:
                self.log.add("would_revert", sha=sha, app=t.app, reason=why)
            else:
                ok2, msg = do_revert(self.remote, sha, why=why)
                self.log.add("reverted" if ok2 else "revert_failed", sha=sha, app=t.app, reason=f"{why} -> {msg}")


def repo_remote(root):
    return git(root, "remote", "get-url", "origin").stdout.strip()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo", default=str(ROOT))
    r.add_argument("--remote-url", help="where main lives (default: origin of --repo)")
    r.add_argument("--kubeconfig", default=str(ROOT / "kubeconfigs" / "diagnoser.kubeconfig"))
    r.add_argument("--context")
    r.add_argument("--state", default=str(ROOT / "watchdog" / "state" / "watchdog.jsonl"))
    r.add_argument("--timeout", type=float, default=300)
    r.add_argument("--min-observe", type=float, default=90)
    r.add_argument("--stable", type=float, default=45)
    r.add_argument("--fail-after", type=float, default=30)
    r.add_argument("--interval", type=float, default=15)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--once", action="store_true")
    r.add_argument("--duration", type=float, default=0)
    r.add_argument("--kill-switch", default=str(ROOT / "watchdog" / "DISABLE"), help="if this file exists the watchdog logs but never reverts")
    a = p.parse_args()
    if not Path(a.kubeconfig).exists():
        print(f"missing {a.kubeconfig}; run: bash scripts/make-kubeconfig.sh diagnoser 8h", file=sys.stderr)
        return 2
    wd = Watchdog(a.repo, a.remote_url or repo_remote(a.repo), Log(a.state), kubectl_health(a.kubeconfig, a.context),
                  timeout=a.timeout, min_observe=a.min_observe, stable=a.stable, fail_after=a.fail_after,
                  dry_run=a.dry_run, kill_switch=a.kill_switch)
    print(f"watchdog started dry_run={a.dry_run} timeout={a.timeout}s remote={wd.remote}", flush=True)
    t0 = time.time()
    while True:
        try:
            wd.step()
        except Exception as e:
            print(f"warn: pass failed: {e}", file=sys.stderr, flush=True)
        if a.once or (a.duration and time.time() - t0 >= a.duration):
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
