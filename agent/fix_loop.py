#!/usr/bin/env python3
"""Fix loop (Phase 7): a diagnosed incident -> LLM patch -> independent gate -> pull request (L1). Never merges.

    python agent/fix_loop.py run  [--dry-run] [--once] [--incidents PATH] [--state PATH] [--fastpath-state PATH]
    python agent/fix_loop.py run  --interval 10 --max-attempts 2 --max-prs-per-day 3 --sandbox

Reads `diagnosed` events (Phase 6) from the incident log. Acts only when the diagnosis is a fault with a fixable
remediation class and the target app is the incident's app or one it affects. If --fastpath-state is given it acts only
on incidents the deterministic fast path declined (playbooks first, principle 3) and counts the fast path's PRs in
its budgets.

Identity: the fixer's fine-grained PAT (env FIXER_GITHUB_TOKEN) and OPENAI_API_KEY. No cluster access of any kind.
The author (fixagent.py) and the checker (policy/gate.py, a separate process) share no code.

Retry budget per incident: --max-attempts patch proposals (default 2; the second sees why the first was rejected), then
the incident escalates. Per app: one open PR at a time, --max-prs-per-day in 24h, and no new PR within --cooldown seconds
of one of our fixes being merged (a merged fix that did not cure the incident is not stacked on).
Exit codes: 0 ok, 2 refused to start.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).parent))
import yaml  # noqa: E402
import fixagent  # noqa: E402
import playbooks  # noqa: E402
from budget import Budget  # noqa: E402
from fastpath import (FastPath, State, TOKEN_ENV, gate_summary, latest_incidents, md_code, render_diff,  # noqa: E402
                      repo_from_origin, run_gate, validate_plan)
from github import GitHub, GitHubError, preflight  # noqa: E402
from gitrepo import GitRepo  # noqa: E402
from incidents import read_events  # noqa: E402

sys.path.insert(0, str(ROOT / "policy"))
import risk  # noqa: E402

COOLDOWN_S = 3600


def diagnoses(path):
    """incident id -> latest `diagnosed` event."""
    out = {}
    for ev in read_events(path):
        if ev.get("event") == "diagnosed" and ev.get("incident_id"):
            out[ev["incident_id"]] = ev
    return out


def known_apps(repo):
    return sorted({p.split("/")[1] for p in repo.ls("apps/") if p.count("/") >= 2})


def render_app(repo, app):
    """kustomize build of apps/<app>'s healthy overlay at the repo ref (offline), optionally with files swapped in."""
    return build_app(repo, app, {})


def build_app(repo, app, files):
    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise RuntimeError("kubectl not found (needed for the kustomize build)")
    tmp = tempfile.mkdtemp(prefix="sentinel-render-")
    try:
        repo.extract([f"apps/{app}"], tmp)
        for p, text in files.items():
            (Path(tmp) / p).write_text(text, encoding="utf-8", newline="")
        r = subprocess.run([kubectl, "kustomize", str(Path(tmp) / "apps" / app / "overlays" / "healthy")], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"kustomize build failed: {r.stderr.strip()[:300]}")
        return r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def llm_pr_body(inc, diag, patch, plan, diff, gate_text, attempts, model):
    top = diag["diagnosis"]["hypotheses"][0]
    sigs = "\n".join(f"- {md_code(s['type'], 40)} on {md_code(s['app'], 60)}: {md_code(s['detail'])}" for s in inc["signals"])
    return "\n".join([
        f"**Automated fix proposal written by an LLM** (model {md_code(model, 40)}, fix class {md_code(plan.playbook)}). "
        "Autonomy level L1: nothing is applied until a human merges this. The author did not validate its own patch: "
        "the independent gate below did.", "",
        "### Incident", f"- id: {md_code(inc['id'])}", f"- app: {md_code(inc['app'])}",
        f"- opened: {datetime.fromtimestamp(inc['opened_at'], timezone.utc).isoformat(timespec='seconds')}", "", "Signals:", sigs, "",
        "### Diagnosis (Phase 6, read-only agent)",
        f"- category: {md_code(top['category'], 40)}, target app {md_code(top['target_app'], 60)}, confidence {top['confidence']:.2f}",
        f"- root cause (model text, untrusted): {md_code(top['root_cause'], 400)}", "",
        "### Why this change (model text, untrusted)", f"- {md_code(patch['rationale'], 600)}",
        f"- author confidence {patch['confidence']:.2f}; patch proposals used: {attempts}",
        *(["- the author flagged possible prompt injection in the data it read: review with extra care"] if patch["injection_suspected"] else []), "",
        "### Change", *[f"- {c}" for c in plan.changes], "", "```diff", diff, "```", "",
        "### Checks run before opening this PR",
        "- the edits are exact, unique find/replace changes to existing manifests of the target app",
        "- kustomize build of the app as Flux deploys it, and the built output changed",
        gate_text, "",
        "### Rollback", "Revert this PR. Flux reconciles within about a minute. The host watchdog also reverts a merged fix that "
        "leaves the app unhealthy."])


class FixLoop(FastPath):
    def __init__(self, repo, state, gh, *, agent, incidents_path, fetch_url, fastpath_state=None, max_attempts=2,
                 cooldown_s=COOLDOWN_S, render=None, build=None, **kw):
        super().__init__(repo, state, gh, incidents_path=incidents_path, fetch_url=fetch_url, settle_s=0, **kw)
        self.agent, self.fp_state, self.max_attempts, self.cooldown_s = agent, fastpath_state, max_attempts, cooldown_s
        self.render = render or (lambda app: render_app(repo, app))
        self.build = build or (lambda app, files: build_app(repo, app, files))
        self.diag = {}

    def candidates(self, now):
        self.diag = diagnoses(self.state_incidents)
        for inc in latest_incidents(self.state_incidents).values():
            d = self.diag.get(inc["id"])
            if inc["state"] != "open" or d is None or self._done(inc["id"]) or not self._actionable(inc, d):
                continue
            if self.fp_state is not None:
                self.fp_state.mem = State(self.fp_state.path).mem if self.fp_state.path else self.fp_state.mem
                if not any(e.get("incident") == inc["id"] and e["event"] == "declined" for e in self.fp_state.mem):
                    continue      # the deterministic fast path has not given up on this one (or already opened a PR)
            yield inc

    @staticmethod
    def _actionable(inc, d):
        """Only a confident fault diagnosis with a fixable class whose target is the incident's app or one it affects.
        'none', 'escalate' and a low-confidence diagnosis never reach the LLM fixer."""
        if d.get("action") != "diagnosed" or not d.get("diagnosis"):
            return False
        top = d["diagnosis"]["hypotheses"][0]
        return (top["category"] != "none" and d["diagnosis"]["remediation_class"] in fixagent.FIXABLE
                and top["target_app"] in {inc["app"], *inc.get("affected_apps", [])})

    def budget_problem(self, app, now):
        problem = self.state.budget_problem(app, now, self.max_prs)
        if not problem and self.fp_state is not None:
            problem = self.fp_state.budget_problem(app, now, self.max_prs)
        if problem:
            return problem
        merged = [e for e in self.state.mem + (self.fp_state.mem if self.fp_state else []) if e["event"] == "pr_merged" and e.get("app") == app
                  and now - e["ts"] < self.cooldown_s]
        if merged:
            return f"a sentinel fix for {app} was merged {int(now - merged[-1]['ts'])}s ago and the incident persists; not stacking another"
        return None

    def handle(self, inc, now):
        d = self.diag[inc["id"]]
        diag = d["diagnosis"]
        top = diag["hypotheses"][0]
        app, rem = top["target_app"], diag["remediation_class"]
        problem = self.budget_problem(app, now)
        if problem:
            return self.decline(inc, problem)
        base = self.repo.head()
        toolbox = fixagent.FixToolbox(self.repo, known_apps(self.repo), self.render)
        feedback, declines, model = None, [], None
        for attempt in range(1, self.max_attempts + 1):
            res = self.agent.propose(inc, diag, self.repo, toolbox, feedback)
            model = res["model"]
            if not self.dry_run:
                self.state.add("fix_attempt", incident=inc["id"], app=app, attempt=attempt, status=res["status"], reason=res["reason"],
                               tokens=res["tokens"], model=model, injection_suspected=res["injection_suspected"])
            if res["status"] == "error":
                if self.dry_run:
                    return self.decline(inc, res["reason"])
                self.state.add("error", incident=inc["id"], app=app, reason=f"{res['reason']}: {res.get('error')}")
                self.out(f"error     {inc['id']}: {res['reason']} ({res.get('error')})")
                return
            if res["status"] != "patch":
                return self.decline(inc, f"fix agent: {res['reason']}")
            patch = res["patch"]
            plan = playbooks.Plan(playbook=fixagent.fix_class(rem), app=app, files=patch["files"],
                                  title=f"sentinel({app}): {rem.replace('_', ' ')} (LLM fix)",
                                  rationale="LLM-authored patch", changes=[f"edit {p}" for p in sorted(patch["files"])])
            reject = self._check(plan, app)
            gate_text = ""
            if reject is None:
                verdict = self.gate_fn(plan)
                meta = {"playbook": plan.playbook, "app": app, "files": plan.files, "old": {p: self.repo.read(p) for p in plan.files}}
                decision = risk.decide(verdict, meta, self.autonomy)
                if not self.dry_run:
                    self.state.add("gate", incident=inc["id"], app=app, playbook=plan.playbook, verdict=verdict["status"],
                                   level=decision["level"], failed=verdict.get("failed", []), score=decision["score"], attempt=attempt)
                if verdict["status"] == "incomplete":
                    return self.decline(inc, f"gate incomplete (a safety check could not run): {', '.join(verdict.get('skipped', []))}")
                if decision["level"] in ("blocked", "L0"):
                    reject = f"gate/policy: {decision['level']}: " + "; ".join(decision["reasons"]) + " | " + " | ".join(
                        f"{c['name']}: {'; '.join(c['detail'])}" for c in verdict["checks"] if c["status"] == "fail")
                    if decision["level"] == "L0":
                        return self.decline(inc, reject)     # a policy ceiling is not something a retry can change
                else:
                    gate_text = gate_summary(verdict, decision)
            if reject is not None:
                declines.append(reject)
                self.out(f"rejected  {inc['id']} attempt {attempt}: {reject[:200]}")
                feedback = reject
                continue
            diff = render_diff(plan, self.repo)
            body = llm_pr_body(inc, d, patch, plan, diff, gate_text, attempt, model)
            if self.dry_run:
                self._dry_seen.add(inc["id"])
                self.out(f"dry-run   {inc['id']}: would open PR {plan.title!r} ({plan.playbook}, attempt {attempt})\n{diff}")
                return
            return self._open(inc, plan, base, body)
        return self.decline(inc, f"no acceptable patch in {self.max_attempts} attempts: " + " || ".join(x[:200] for x in declines))

    def _check(self, plan, app):
        """Mechanical checks before the gate: it builds the way Flux builds it, and the built output really changed."""
        err = validate_plan(plan, self.repo)
        if err:
            return f"validation: {err}"
        try:
            if self.build(app, plan.files) == self.build(app, {}):
                return "the patch does not change the manifests Flux would apply (built output is identical)"
        except RuntimeError as e:
            return f"validation: {e}"
        return None

    def _open(self, inc, plan, base, body):
        branch = f"sentinel/{plan.playbook}/{plan.app}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        try:
            number, url = self.gh.open_fix_pr(base_branch="main", base_sha=base, branch=branch, files=plan.files,
                                              message=f"{plan.title}\n\nIncident {inc['id']}, fix class {plan.playbook}.",
                                              title=plan.title, body=body)
        except (GitHubError, ValueError) as e:
            self.state.add("error", incident=inc["id"], app=inc["app"], reason=str(e))
            self.out(f"error     {inc['id']}: {e}")
            return
        self.state.add("pr_opened", incident=inc["id"], app=plan.app, playbook=plan.playbook, pr=number, url=url,
                       branch=branch, base_sha=base, incident_opened_at=inc["opened_at"])
        self.out(f"pr_opened {inc['id']} app={plan.app} class={plan.playbook} {url}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--incidents", default=str(ROOT / "agent" / "state" / "incidents.jsonl"))
    r.add_argument("--state", default=str(ROOT / "agent" / "state" / "fixloop.jsonl"))
    r.add_argument("--fastpath-state", help="act only on incidents the deterministic fast path declined; count its PRs")
    r.add_argument("--repo", help="owner/name (default: parsed from the origin remote)")
    r.add_argument("--fetch-url")
    r.add_argument("--interval", type=float, default=10.0)
    r.add_argument("--max-attempts", type=int, default=2)
    r.add_argument("--max-prs-per-day", type=int, default=3)
    r.add_argument("--cooldown", type=float, default=COOLDOWN_S)
    r.add_argument("--min-confidence", type=float, default=fixagent.MIN_CONFIDENCE)
    r.add_argument("--dry-run", action="store_true", help="propose and gate, print the diff, open nothing (no GitHub token needed)")
    r.add_argument("--sandbox", action="store_true")
    r.add_argument("--once", action="store_true")
    r.add_argument("--duration", type=float, default=0)
    r.add_argument("--allow-admin-token", action="store_true", help="NOT recommended")
    a = p.parse_args()

    repo_name = a.repo or repo_from_origin(ROOT)
    if not repo_name:
        print("cannot work out owner/name from the origin remote; pass --repo", file=sys.stderr); return 2
    fetch_url = a.fetch_url or f"https://github.com/{repo_name}.git"
    try:
        budget = Budget.from_env()
    except ValueError as e:
        print(f"budget misconfigured: {e}", file=sys.stderr); return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr); return 2
    gh = None
    if not a.dry_run:
        token = os.environ.get(TOKEN_ENV)
        if not token:
            print(f"{TOKEN_ENV} is not set (see docs/phase4.md); use --dry-run to propose without a token", file=sys.stderr); return 2
        gh = GitHub(repo_name, token)
        problems = preflight(gh, "main", a.allow_admin_token)
        if problems:
            print("REFUSING to start:\n  " + "\n  ".join(problems), file=sys.stderr); return 2
    repo = GitRepo(ROOT)
    state = State(a.state)
    fps = State(a.fastpath_state) if a.fastpath_state else None
    agent = fixagent.FixAgent(min_confidence=a.min_confidence, budget=budget)
    loop = FixLoop(repo, state, gh, agent=agent, incidents_path=a.incidents, fetch_url=fetch_url, fastpath_state=fps,
                   max_attempts=a.max_attempts, cooldown_s=a.cooldown, max_prs_per_day=a.max_prs_per_day, dry_run=a.dry_run, sandbox=a.sandbox)
    state.add("started", dry_run=a.dry_run, repo=repo_name)
    print(f"fix_loop started repo={repo_name} dry_run={a.dry_run} incidents={a.incidents} run={budget.run_id!r}", flush=True)
    t0 = time.time()
    while True:
        try:
            loop.step()
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
