#!/usr/bin/env python3
"""Deterministic fast path (Phase 4): known incident -> playbook -> validated change -> pull request. No LLM.

    python agent/fastpath.py run  [--dry-run] [--once] [--incidents PATH] [--state PATH] [--interval 10] [--settle 30]
    python agent/fastpath.py plan INCIDENT_ID [--incidents PATH]       # show the plan and diff, change nothing

Reads incidents from the detector's JSONL log, plans against a fetched snapshot of origin/main, and opens a PR with
the fixer's fine-grained PAT (env FIXER_GITHUB_TOKEN). Autonomy L1: it NEVER merges; a human approves.
It has no cluster access at all (the only external program besides git is `kubectl kustomize`, which is offline).

Hard rails (it refuses to start otherwise): token valid and not admin, main protected on GitHub.
Per-incident: one PR at most, then never again for that incident. Per-app: at most --max-prs-per-day in 24h and
never a second PR while one is still open.
Exit codes: 0 ok, 2 refused to start.
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
import yaml  # noqa: E402
from github import GitHub, GitHubError, preflight  # noqa: E402
from gitrepo import GitRepo  # noqa: E402
from incidents import read_events  # noqa: E402
import playbooks  # noqa: E402
import feedback as feedback_mod  # noqa: E402
sys.path.insert(0, str(ROOT / "policy"))
import risk  # noqa: E402  (policy/risk engine; the gate itself is only ever run as a separate process)
import controls  # noqa: E402  (Phase 9: global kill switch)

TOKEN_ENV = "FIXER_GITHUB_TOKEN"
MAX_RETRIES = 3


# --- state -------------------------------------------------------------------------
class State:
    """Append-only JSONL of what the fast path did. The source of truth for 'already handled' and for budgets."""
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.mem = []
        if self.path and self.path.exists():
            self.mem = read_events(self.path)

    def add(self, event, **kw):
        ev = {"ts": time.time(), "event": event, **kw}
        self.mem.append(ev)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        return ev

    def handled(self, incident_id):
        return any(e.get("incident") == incident_id and e["event"] in ("pr_opened", "declined", "dry_run") for e in self.mem) \
            or sum(1 for e in self.mem if e.get("incident") == incident_id and e["event"] == "error") >= MAX_RETRIES

    def prs(self):
        """pr_opened events with their latest outcome folded in."""
        out = {}
        for e in self.mem:
            if e["event"] == "pr_opened":
                out[e["pr"]] = dict(e, status="open")
            elif e["event"] in ("pr_merged", "pr_closed") and e.get("pr") in out:
                out[e["pr"]]["status"] = e["event"][3:]
        return list(out.values())

    def budget_problem(self, app, now, max_per_day):
        mine = [p for p in self.prs() if p["app"] == app]
        if any(p["status"] == "open" for p in mine):
            return f"a sentinel PR for {app} is still open (#{[p['pr'] for p in mine if p['status'] == 'open'][0]})"
        if sum(1 for p in mine if now - p["ts"] < 86400) >= max_per_day:
            return f"{app} already got {max_per_day} fix PRs in the last 24h (retry budget)"
        return None


# --- helpers ---------------------------------------------------------------------------
def latest_incidents(path):
    last = {}
    for ev in read_events(path):
        if "incident" in ev:
            last[ev["incident"]["id"]] = ev["incident"]
    return last


def repo_from_origin(root):
    url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def md_code(s, n=200):
    """Untrusted text goes into the PR body only as a code span: no @mentions, links or markdown from it."""
    s = re.sub(r"\s+", " ", str(s)).replace("`", "'")[:n]
    return f"`{s}`"


def render_diff(plan, repo):
    out = []
    for path, new in sorted(plan.files.items()):
        old = repo.read(path) or ""
        out.extend(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm=""))
    return "\n".join(out)


def validate_plan(plan, repo):
    """Independent-ish sanity gate (the real gate is Phase 5): the changed app must still build with kustomize, the
    way Flux builds it, and playbook-specific expectations must hold. Returns an error string or None."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return "kubectl not found (needed for the kustomize build check)"
    for p in plan.files:
        if not p.startswith(f"apps/{plan.app}/"):
            return f"plan touches {p}, outside apps/{plan.app}/"
    tmp = tempfile.mkdtemp(prefix="sentinel-validate-")
    try:
        repo.extract([f"apps/{plan.app}", "infra/flux/kustomizations.yaml"], tmp)
        flux = repo.read("infra/flux/kustomizations.yaml") or ""
        target = next((d["spec"]["path"] for d in yaml.safe_load_all(flux) if d and d.get("metadata", {}).get("name") == plan.app), None)
        if not target or not str(target).startswith(f"./apps/{plan.app}/"):
            return f"no Flux Kustomization deploys apps/{plan.app}/ (path={target!r})"
        for p, text in plan.files.items():
            (Path(tmp) / p).write_text(text, encoding="utf-8", newline="")
        r = subprocess.run([kubectl, "kustomize", str(Path(tmp) / target)], capture_output=True, text=True)
        if r.returncode != 0:
            return f"kustomize build failed: {r.stderr.strip()[:300]}"
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
        if not docs:
            return "kustomize build is empty"
        want = plan.expect.get("memory_limit_mi")
        if want is not None:
            got = [c.get("resources", {}).get("limits", {}).get("memory") for d in docs if d.get("kind") == "Deployment"
                   for c in d["spec"]["template"]["spec"]["containers"]]
            if got != [f"{want}Mi"]:
                return f"built memory limit is {got}, expected ['{want}Mi']"
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_gate(plan, repo, sandbox=False):
    """Ask the independent validation gate (policy/gate.py, separate process, shares no code with the playbooks) for a
    verdict on the plan. Anything other than a clean JSON verdict counts as 'blocked'."""
    cmd = [sys.executable, str(ROOT / "policy" / "gate.py"), "check", "--repo", str(repo.path), "--base-ref", repo.ref,
           "--changes-json", "-", "--json"] + (["--sandbox"] if sandbox else [])
    r = subprocess.run(cmd, input=json.dumps(plan.files), capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"status": "blocked", "failed": ["gate-error"], "skipped": [], "apps": [], "changed_lines": 0,
                "checks": [{"name": "gate-error", "status": "fail", "detail": [(r.stderr or r.stdout or "no output").strip()[:300]]}]}


def gate_summary(v, decision):
    lines = [f"- gate verdict: **{v['status']}**; autonomy decision: **{decision['level']}** (risk score {decision['score']})"]
    for c in v["checks"]:
        lines.append(f"  - {c['status']}: {c['name']}" + (f" ({md_code('; '.join(c['detail']), 160)})" if c["detail"] else ""))
    lines.append(f"  - risk reasons: {md_code('; '.join(decision['reasons']), 240)}")
    return "\n".join(lines)


def pr_body(inc, plan, validation, diff, declines, gate_text=""):
    sigs = "\n".join(f"- {md_code(s['type'], 40)} on {md_code(s['app'], 60)}: {md_code(s['detail'])}" for s in inc["signals"])
    return "\n".join([
        f"**Automated fix proposal** (deterministic playbook {md_code(plan.playbook)}, no LLM). Autonomy level L1: nothing "
        "is applied until a human merges this.", "",
        "### Incident", f"- id: {md_code(inc['id'])}", f"- app: {md_code(inc['app'])}"
        + (f" (also affecting {', '.join(md_code(a, 60) for a in inc['affected_apps'])})" if inc["affected_apps"] else ""),
        f"- opened: {datetime.fromtimestamp(inc['opened_at'], timezone.utc).isoformat(timespec='seconds')}", "", "Signals:", sigs, "",
        "### Why this playbook", plan.rationale, "",
        "### Change", *[f"- {c}" for c in plan.changes], "", "```diff", diff, "```", "",
        "### Checks run before opening this PR",
        f"- kustomize build of the app as Flux deploys it: {validation}",
        *([gate_text] if gate_text else []),
        "- the commit touches only files under the app's own `base/` and `overlays/` (playbook rule)", "",
        "### Rollback", "Revert this PR. Flux reconciles within about a minute.",
        *(["", "<details><summary>Playbooks that did not apply</summary>", "",
           *[f"- {n}: {md_code(r, 300)}" for n, r in declines], "", "</details>"] if declines else [])])


# --- the loop ------------------------------------------------------------------------------
class FastPath:
    def __init__(self, repo, state, gh, *, incidents_path, fetch_url, settle_s=30.0, max_prs_per_day=3, dry_run=False, cfg=None, out=print,
                 gate_fn=None, sandbox=False, autonomy=None, feedback=None):
        self.repo, self.state, self.gh, self.fetch_url = repo, state, gh, fetch_url
        self.state_incidents = incidents_path
        self.settle_s, self.max_prs, self.dry_run = settle_s, max_prs_per_day, dry_run
        self.cfg, self.out = cfg or playbooks.Config(), out
        self._dry_seen = set()
        self.gate_fn = gate_fn or (lambda plan: run_gate(plan, repo, sandbox))
        self.autonomy = autonomy
        self.feedback = feedback          # Phase 8: human decisions on earlier PRs (feedback.FeedbackStore) or None

    def ingest_feedback(self):
        """Fold PR outcomes into the feedback store so a rejection takes effect before the next proposal."""
        if self.feedback is not None and not self.dry_run:
            self.feedback.ingest(latest_incidents(self.state_incidents), [self._outcome_events()])

    def _outcome_events(self):
        return list(self.state.mem)

    def rejected_before(self, inc, fix_class):
        """Standing human rejection of this fix class for this incident signature, or None."""
        return self.feedback.rejection(feedback_mod.signature(inc), fix_class) if self.feedback is not None else None

    def reject_reason(self, inc, rej):
        return (f"a human rejected this fix class for {feedback_mod.signature(inc)} before ({rej.get('url') or rej.get('via')}); "
                "not proposing it again until it is cleared (python agent/feedback.py forget)")

    def reconcile(self):
        """Fold PR outcomes (merged / closed) into state and tidy up our own branch."""
        if self.dry_run or not self.gh:
            return
        for p in self.state.prs():
            if p["status"] != "open":
                continue
            try:
                info = self.gh.pull(p["pr"])
            except GitHubError as e:
                self.out(f"warn: cannot read PR #{p['pr']}: {e}"); continue
            if info.get("state") == "closed":
                ev = "pr_merged" if info.get("merged") else "pr_closed"
                self.state.add(ev, pr=p["pr"], app=p["app"], incident=p["incident"], playbook=p["playbook"])
                self.out(f"{ev[3:]:<9} PR #{p['pr']} ({p['app']}, {p['playbook']})")
                try:
                    self.gh.delete_own_branch(p["branch"])
                except GitHubError:
                    pass

    def candidates(self, now):
        for inc in latest_incidents(self.state_incidents).values():
            if inc["state"] == "open" and now - inc["opened_at"] >= self.settle_s and not self._done(inc["id"]):
                yield inc

    def _done(self, iid):
        return iid in self._dry_seen if self.dry_run else self.state.handled(iid)

    def step(self, now=None):
        now = time.time() if now is None else now
        if controls.killed():             # Phase 9 kill switch: no new PRs, nothing proposed. PR outcomes are still read so state stays accurate.
            self.reconcile()
            return 0
        self.reconcile()
        self.ingest_feedback()
        todo = list(self.candidates(now))
        if not todo:
            return 0
        self.repo.fetch(self.fetch_url)
        for inc in todo:
            self.handle(inc, now)
        return len(todo)

    def decline(self, inc, reason, declines=()):
        self.out(f"declined  {inc['id']}: {reason}")
        self.out(f"escalate  {inc['id']} app={inc['app']}: no deterministic fix (the escalator pages a human: agent/escalate.py)")
        if self.dry_run:
            self._dry_seen.add(inc["id"])
        else:
            self.state.add("declined", incident=inc["id"], app=inc["app"], reason=reason, declines=list(declines))

    def handle(self, inc, now):
        base = self.repo.head()
        problem = self.state.budget_problem(inc["app"], now, self.max_prs)
        if problem:
            return self.decline(inc, problem)
        plan, declines = playbooks.select(inc, self.repo, self.cfg)
        if not plan:
            return self.decline(inc, "; ".join(f"{n}: {r}" for n, r in declines), declines)
        rej = self.rejected_before(inc, plan.playbook)
        if rej:
            return self.decline(inc, self.reject_reason(inc, rej), declines)
        err = validate_plan(plan, self.repo)
        if err:
            return self.decline(inc, f"plan from {plan.playbook} failed validation: {err}", declines)
        verdict = self.gate_fn(plan)
        meta = {"playbook": plan.playbook, "app": plan.app, "files": plan.files, "old": {p: self.repo.read(p) for p in plan.files}}
        decision = risk.decide(verdict, meta, self.autonomy)
        if not self.dry_run:
            self.state.add("gate", incident=inc["id"], app=plan.app, playbook=plan.playbook, verdict=verdict["status"],
                       level=decision["level"], failed=verdict.get("failed", []), score=decision["score"])
        if decision["level"] in ("blocked", "L0"):
            self.out(f"gate      {inc['id']}: {decision['level']} ({'; '.join(decision['reasons'])})")
            return self.decline(inc, f"gate/policy: {decision['level']}: {'; '.join(decision['reasons'])}", declines)
        diff = render_diff(plan, self.repo)
        body = pr_body(inc, plan, "ok", diff, declines, gate_summary(verdict, decision))
        if self.dry_run:
            self._dry_seen.add(inc["id"])
            self.out(f"dry-run   {inc['id']}: would open PR {plan.title!r} from {plan.playbook}\n{diff}")
            return
        branch = f"sentinel/{plan.playbook}/{plan.app}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        try:
            number, url = self.gh.open_fix_pr(base_branch="main", base_sha=base, branch=branch, files=plan.files,
                                              message=f"{plan.title}\n\nIncident {inc['id']}, playbook {plan.playbook}.",
                                              title=plan.title, body=body)
        except (GitHubError, ValueError) as e:
            self.state.add("error", incident=inc["id"], app=inc["app"], reason=str(e))
            self.out(f"error     {inc['id']}: {e}")
            return
        self.state.add("pr_opened", incident=inc["id"], app=inc["app"], playbook=plan.playbook, pr=number, url=url,
                       branch=branch, base_sha=base, incident_opened_at=inc["opened_at"])
        self.out(f"pr_opened {inc['id']} app={inc['app']} playbook={plan.playbook} {url}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--incidents", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
    common.add_argument("--repo", help="owner/name (default: parsed from the origin remote)")
    common.add_argument("--fetch-url", help="where to read main from (default: https URL of the repo, anonymous)")
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--state", default=str(ROOT / "agent" / "state" / "fastpath.jsonl"))
    r.add_argument("--interval", type=float, default=10.0)
    r.add_argument("--settle", type=float, default=30.0, help="wait this long after an incident opens so late signals are in")
    r.add_argument("--max-prs-per-day", type=int, default=3)
    r.add_argument("--feedback", default=str(ROOT / "agent" / "state" / "feedback.jsonl"), help="human decisions on earlier PRs (Phase 8)")
    r.add_argument("--dry-run", action="store_true", help="plan and validate, print the diff, open nothing (no token needed)")
    r.add_argument("--sandbox", action="store_true", help="also reproduce-then-fix in a vcluster (slow; needs a cluster and vcluster)")
    r.add_argument("--once", action="store_true")
    r.add_argument("--duration", type=float, default=0)
    r.add_argument("--allow-admin-token", action="store_true", help="NOT recommended: token belongs to a repo admin")
    pl = sub.add_parser("plan", parents=[common]); pl.add_argument("incident_id")
    a = p.parse_args()

    repo_name = a.repo or repo_from_origin(ROOT)
    if not repo_name:
        print("cannot work out owner/name from the origin remote; pass --repo", file=sys.stderr); return 2
    fetch_url = a.fetch_url or f"https://github.com/{repo_name}.git"
    repo = GitRepo(ROOT)

    if a.cmd == "plan":
        inc = latest_incidents(a.incidents).get(a.incident_id)
        if not inc:
            print(f"no incident {a.incident_id} in {a.incidents}", file=sys.stderr); return 2
        repo.fetch(fetch_url)
        plan, declines = playbooks.select(inc, repo)
        if not plan:
            print("no playbook applies:\n" + "\n".join(f"  {n}: {r}" for n, r in declines)); return 1
        err = validate_plan(plan, repo)
        print(f"{plan.playbook}: {plan.title}\n{plan.rationale}\nvalidation: {err or 'ok'}\n\n{render_diff(plan, repo)}")
        return 1 if err else 0

    gh = None
    if not a.dry_run:
        token = os.environ.get(TOKEN_ENV)
        if not token:
            print(f"{TOKEN_ENV} is not set (fine-grained PAT, Contents + Pull requests write, this repo only; see docs/phase4.md)\n"
                  "use --dry-run to plan without a token", file=sys.stderr); return 2
        gh = GitHub(repo_name, token)
        problems = preflight(gh, "main", a.allow_admin_token)
        if problems:
            print("REFUSING to start:\n  " + "\n  ".join(problems), file=sys.stderr); return 2
    state = State(a.state)
    fp = FastPath(repo, state, gh, incidents_path=a.incidents, fetch_url=fetch_url, settle_s=a.settle, max_prs_per_day=a.max_prs_per_day, dry_run=a.dry_run, sandbox=a.sandbox,
                  feedback=feedback_mod.FeedbackStore(a.feedback))
    state.add("started", dry_run=a.dry_run, repo=repo_name)
    print(f"fastpath started repo={repo_name} dry_run={a.dry_run} incidents={a.incidents}", flush=True)
    t0 = time.time()
    while True:
        try:
            fp.step()
        except Exception as e:   # a bad pass must not kill the loop; the incident is retried (bounded by MAX_RETRIES)
            print(f"warn: pass failed: {e}", file=sys.stderr, flush=True)
        if a.once or (a.duration and time.time() - t0 >= a.duration):
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
