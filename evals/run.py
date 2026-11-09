#!/usr/bin/env python3
"""Failure-injection eval runner.

    python evals/run.py list
    python evals/run.py validate
    python evals/run.py run <id ...|all> [--yes] [--dry-run] [--responder CMD] [--detector] [--fastpath | --fixagent] [--approve auto|manual] [--out FILE]
    python evals/run.py summary <results.json>

`run` injects each scenario THROUGH GIT (commits a broken overlay and pushes to origin/main, because Flux
watches main), waits for the symptom, optionally lets a responder (the future agent) diagnose and fix, scores,
then reverts through Git and waits for recovery. See docs/phase2.md.

--detector (Phase 3): starts agent/watch.py (read-only diagnoser kubeconfig) before each injection and scores the
incidents it opens: exactly one on the true target app per failure, none for the decoy.

--fastpath (Phase 4, implies --detector): also runs agent/fastpath.py against the detector's incidents. It opens a PR
with the fixer PAT (env FIXER_GITHUB_TOKEN). The runner then plays the human approver: --approve auto (default)
fast-forwards main to the PR branch and pushes it with YOUR git credentials (the PR then shows as merged); --approve
manual waits up to APPROVE_TIMEOUT for you to merge it on GitHub. Scored: exactly one PR on the true target app per
failure and none for the decoy, plus whether the cluster recovered because of the merged PR.

--fixagent (Phase 7, implies --detector): instead of the fast path, runs agent/diagnose_loop.py (Phase 6) and agent/fix_loop.py,
so the LLM fix agent is measured on every scenario (playbooks are not consulted). Needs FIXER_GITHUB_TOKEN and
OPENAI_API_KEY. Same PR scoring and --approve behaviour as --fastpath; a PR for the decoy is a false fix.

Responder protocol (optional): a shell command run from the repo root with env EVAL_SCENARIO_ID,
EVAL_INJECT_APP, EVAL_OBSERVE_APP, EVAL_INJECT_SHA. It prints, as its LAST stdout line, JSON:
  {"action": "fix"|"none"|"escalate",
   "diagnosis": {"category": "...", "target_app": "...", "root_cause": "free text"}}
Without a responder only harness health is judged (symptom visible, revert recovers); fix and diagnosis
metrics are reported as null, never as 0.
"""
import argparse, atexit, datetime, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from scoring import aggregate, score_detection, score_diagnosis, score_fastpath, score_scenario  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from incidents import read_events, summarize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCEN_DIR = ROOT / "evals" / "scenarios"
CTX = os.environ.get("EDGELOOP_CONTEXT", "docker-desktop")
POLL = 3
SYMPTOM_TIMEOUT = int(os.environ.get("SYMPTOM_TIMEOUT", "300"))
RECOVERY_TIMEOUT = int(os.environ.get("RECOVERY_TIMEOUT", "300"))
SYMPTOM_TYPES = {"container_reason", "restarts", "not_ready", "stays_healthy"}
CATEGORIES = {"oom", "bad_config", "app_crash", "bad_image", "bad_env_var", "probe_failure", "dependency_outage", "none"}


# --- scenarios ---------------------------------------------------------------
def load_scenarios():
    out = {}
    for f in sorted(SCEN_DIR.glob("*.yaml")):
        s = yaml.safe_load(f.read_text(encoding="utf-8"))
        s["_file"] = f.name
        out[s.get("id", f.stem)] = s
    return out


def validate(scns):
    errs = []
    for sid, s in scns.items():
        local = []
        def e(msg): local.append(f"{s['_file']}: {msg}")
        if sid != Path(s["_file"]).stem: e(f"id '{sid}' must match file name")
        for k in ("description", "inject", "observe", "recovery", "expected"):
            if k not in s: e(f"missing '{k}'")
        if local:
            errs.extend(local); continue
        ov = ROOT / "apps" / s["inject"]["app"] / "overlays" / s["inject"]["overlay"] / "kustomization.yaml"
        if not ov.is_file(): e(f"overlay not found: {ov.relative_to(ROOT)}")
        if not (ROOT / "apps" / s["inject"]["app"] / "overlays" / "healthy" / "kustomization.yaml").is_file():
            e("inject app has no healthy overlay")
        if s["observe"]["symptom"]["type"] not in SYMPTOM_TYPES: e(f"unknown symptom type {s['observe']['symptom']['type']}")
        if s["expected"]["action"] not in ("fix", "none"): e("expected.action must be fix|none")
        d = s["expected"].get("diagnosis", {})
        if d.get("category") not in CATEGORIES: e(f"unknown diagnosis category {d.get('category')}")
        if "target_app" not in d: e("expected.diagnosis.target_app missing")
        for c in d.get("accept_categories", []):
            if c not in CATEGORIES or c == "none": e(f"accept_categories has invalid category {c!r}")
        if (s["expected"]["action"] == "none") != (d.get("category") == "none"): e("action 'none' must pair with category 'none'")
        for a in s["recovery"]["apps"]:
            if not (ROOT / "apps" / a).is_dir(): e(f"recovery app '{a}' not in apps/")
    have = {s["expected"]["diagnosis"]["category"] for s in scns.values() if "expected" in s}
    for need in sorted(CATEGORIES - have): errs.append(f"no scenario covers category '{need}'")
    return errs


# --- cluster / git helpers -----------------------------------------------------
def run(cmd, check=True, **kw):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def kget(*args):
    r = run(["kubectl", "--context", CTX, *args, "-o", "json"], check=False)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"items": []}


def pods(app):
    return kget("-n", "apps", "get", "pods", "-l", f"app={app}")["items"]


def reasons(pod):
    out = set()
    for cs in pod.get("status", {}).get("containerStatuses", []) or []:
        for st in (cs.get("state", {}), cs.get("lastState", {})):
            for k in ("waiting", "terminated"):
                if k in st and st[k].get("reason"): out.add(st[k]["reason"])
    return out


def restarts(pod):
    return sum(cs.get("restartCount", 0) for cs in pod.get("status", {}).get("containerStatuses", []) or [])


def ready(pod):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []) or [])


def symptom_seen(obs):
    sy, ps = obs["symptom"], pods(obs["app"])
    t = sy["type"]
    if t == "container_reason": return any(reasons(p) & set(sy["reasons"]) for p in ps)
    if t == "restarts": return any(restarts(p) >= sy["min"] for p in ps)
    if t == "not_ready": return bool(ps) and any(not ready(p) for p in ps) and not any(ready(p) for p in ps)
    raise ValueError(t)


def healthy(app):
    """Rolled out, every pod Ready, none terminating, replica count matches spec (and spec > 0)."""
    d = kget("-n", "apps", "get", "deploy", app)
    if "spec" not in d: return False
    sp, st = d["spec"]["replicas"], d["status"]
    if sp < 1 or d["metadata"]["generation"] != st.get("observedGeneration"): return False
    if not (st.get("replicas") == st.get("updatedReplicas") == st.get("readyReplicas") == sp): return False
    ps = pods(app)
    return len(ps) == sp and all(ready(p) and not p["metadata"].get("deletionTimestamp") for p in ps)


def all_healthy(apps): return all(healthy(a) for a in apps)


def wait_for(fn, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn(): return time.time() - t0
        time.sleep(POLL)
    return None


def precheck():
    if run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() != "main": return "must be on main"
    if run(["git", "status", "--porcelain"]).stdout.strip(): return "working tree not clean; commit or stash first"
    run(["git", "fetch", "-q", "origin", "main"])
    if run(["git", "rev-parse", "HEAD"]).stdout != run(["git", "rev-parse", "origin/main"]).stdout:
        return "local main != origin/main; sync first"
    return None


# --- one scenario --------------------------------------------------------------
def overlay(app, name): return ROOT / "apps" / app / "overlays" / name / "kustomization.yaml"


def run_responder(cmd, scn, sha, timeout):
    env = {**os.environ, "EVAL_SCENARIO_ID": scn["id"], "EVAL_INJECT_APP": scn["inject"]["app"],
           "EVAL_OBSERVE_APP": scn["observe"]["app"], "EVAL_INJECT_SHA": sha}
    try:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"action": "escalate", "diagnosis": None, "error": "responder timed out"}
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    try:
        return json.loads(lines[-1])
    except Exception:
        return {"action": "escalate", "diagnosis": None, "error": f"unparseable responder output (exit {r.returncode})"}


DIAGNOSER_KUBECONFIG = ROOT / "kubeconfigs" / "diagnoser.kubeconfig"
SETTLE_TIMEOUT = int(os.environ.get("SETTLE_TIMEOUT", "180"))
PR_TIMEOUT = int(os.environ.get("PR_TIMEOUT", "240"))            # incident -> settle -> PR
APPROVE_TIMEOUT = int(os.environ.get("APPROVE_TIMEOUT", "900"))  # --approve manual: how long a human gets


def start_detector():
    """Start the watcher; returns (process, log_path, errfile) once its baseline pass is done, or raises."""
    log = Path(tempfile.mkdtemp(prefix="edgeloop-incidents-")) / "incidents.jsonl"
    err = open(log.parent / "watch.err", "w+", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "agent" / "watch.py"), "--kubeconfig", str(DIAGNOSER_KUBECONFIG),
                             "--interval", "3", "--log", str(log)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=err)
    atexit.register(lambda: proc.poll() is None and proc.kill())   # never leak the watcher if the runner dies
    t0 = time.time()
    while time.time() - t0 < 60:
        if any(e.get("event") == "watch_started" for e in read_events(log)): return proc, log, err
        if proc.poll() is not None: break
        time.sleep(1)
    proc.kill(); err.seek(0)
    raise RuntimeError("detector did not start: " + (err.read().strip() or "no output") +
                       " (run: bash scripts/make-kubeconfig.sh diagnoser)")


def stop_detector(proc, err):
    proc.terminate()
    try: proc.wait(10)
    except subprocess.TimeoutExpired: proc.kill()
    err.close()


def start_fastpath(incident_log, fixagent=False):
    """Start the fast path (or, with fixagent, the Phase 6 diagnosis loop plus the Phase 7 LLM fix loop) on the detector's
    incident log (own temp state). Returns (process, state_path, errfile, extra_processes)."""
    state = incident_log.parent / ("fixloop.jsonl" if fixagent else "fastpath.jsonl")
    err = open(incident_log.parent / "fastpath.err", "w+", encoding="utf-8")
    extra = []
    if fixagent:
        d = subprocess.Popen([sys.executable, str(ROOT / "agent" / "diagnose_loop.py"), "--incidents", str(incident_log),
                              "--kubeconfig", str(DIAGNOSER_KUBECONFIG), "--interval", "3", "--settle", "20"],
                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=err)
        atexit.register(lambda: d.poll() is None and d.kill())
        extra.append(d)
        cmd = [sys.executable, str(ROOT / "agent" / "fix_loop.py"), "run", "--incidents", str(incident_log), "--state", str(state), "--interval", "3"]
    else:
        cmd = [sys.executable, str(ROOT / "agent" / "fastpath.py"), "run", "--incidents", str(incident_log),
               "--state", str(state), "--interval", "3", "--settle", "20"]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=err)
    atexit.register(lambda: proc.poll() is None and proc.kill())
    t0 = time.time()
    while time.time() - t0 < 60:
        if any(e.get("event") == "started" for e in read_events(state)): return proc, state, err, extra
        if proc.poll() is not None: break
        time.sleep(1)
    proc.kill(); [x.kill() for x in extra]; err.seek(0)
    raise RuntimeError("fast path did not start: " + (err.read().strip() or "no output"))


def stop_fastpath(fp):
    proc, _, err, extra = fp
    for x in [proc, *extra]:
        x.terminate()
        try: x.wait(10)
        except subprocess.TimeoutExpired: x.kill()
    err.close()


def approve_pr(pr, fstate, mode):
    """Plays the human reviewer. Returns True once the PR's change is on origin/main."""
    if mode == "manual":
        print(f"  waiting up to {APPROVE_TIMEOUT}s for you to review and merge {pr['url']}", flush=True)
        return wait_for(lambda: any(e["event"] == "pr_merged" for e in read_events(fstate)), APPROVE_TIMEOUT) is not None
    run(["git", "fetch", "-q", "origin", pr["branch"]])
    run(["git", "merge", "--ff-only", "FETCH_HEAD"])     # fails loudly if main moved: never silently merges something else
    run(["git", "push", "-q", "origin", "main"])
    return True


def run_scenario(scn, responder, responder_timeout, detector=False, fastpath=False, approve="auto", fixagent=False):
    rec = {"id": scn["id"], "expected_action": scn["expected"]["action"], "started": datetime.datetime.now().isoformat(timespec="seconds"),
           "detect_s": None, "notes": [],
           "novel": bool(scn.get("novel")), "adversarial": bool(scn.get("adversarial"))}
    app, apps_to_check = scn["inject"]["app"], scn["recovery"]["apps"]
    if not all_healthy(apps_to_check):
        rec["notes"].append("baseline not healthy; refused to inject")
        rec["score"] = score_scenario(scn, None, None, None, False, None, None, False)
        rec["aborted"] = True
        return rec
    base_restarts = sum(restarts(p) for p in pods(scn["observe"]["app"]))
    fastpath = fastpath or fixagent
    det = start_detector() if (detector or fastpath) else None
    fp = None
    if fastpath:
        try:
            fp = start_fastpath(det[1], fixagent)
        except Exception:
            stop_detector(det[0], det[2]); raise
    healthy_file = overlay(app, "healthy")
    original = healthy_file.read_text(encoding="utf-8")
    shutil.copyfile(overlay(app, scn["inject"]["overlay"]), healthy_file)
    if healthy_file.read_text(encoding="utf-8") == original:
        raise RuntimeError(f"{scn['id']}: injection changes nothing (overlay equals healthy)")
    run(["git", "add", str(healthy_file.relative_to(ROOT))])
    run(["git", "commit", "-q", "-m", f"eval: inject {scn['id']} (will be reverted)"])
    sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    try:
        run(["git", "push", "-q", "origin", "main"])
    except Exception:
        run(["git", "reset", "-q", "--hard", "HEAD~1"])
        raise
    t_push = time.time()
    print(f"  injected {sha[:7]} ({scn['inject']['overlay']} -> {app}); waiting for symptom", flush=True)

    sy = scn["observe"]["symptom"]
    symptom_s = None
    if sy["type"] == "stays_healthy":
        # decoy: observe for the window; a symptom here means the decoy is not a decoy
        bad = wait_for(lambda: not all_healthy(scn["recovery"]["apps"]) or sum(restarts(p) for p in pods(scn["observe"]["app"])) > base_restarts, sy["window"])
        if bad is None: symptom_s = float(sy["window"])
        else: rec["notes"].append("decoy became unhealthy: scenario definition is wrong")
        rec["detect_s"] = None
    else:
        symptom_s = wait_for(lambda: symptom_seen(scn["observe"]), SYMPTOM_TIMEOUT)
        rec["detect_s"] = None if symptom_s is None else round(symptom_s, 1)
    print(f"  symptom: {'observed' if symptom_s is not None else 'NOT observed'}", flush=True)

    got = None; got_action = None; recovered_by_fix = None; ttr = None
    if responder:
        print("  running responder", flush=True)
        t_resp = time.time()
        got = run_responder(responder, scn, sha, responder_timeout)
        got_action = got.get("action")
        rec["responder"] = got
        if got_action == "fix":
            ttr_wait = wait_for(lambda: all_healthy(apps_to_check), RECOVERY_TIMEOUT)
            recovered_by_fix = ttr_wait is not None
            ttr = round(time.time() - t_push, 1) if recovered_by_fix else None
    elif fp:
        fstate = fp[1]
        if scn["expected"]["action"] == "fix":
            wait_for(lambda: any(e["event"] in ("pr_opened", "declined") for e in read_events(fstate)), PR_TIMEOUT * (3 if fixagent else 1))
        pr = next((e for e in read_events(fstate) if e["event"] == "pr_opened"), None)
        got_action = "none"
        if pr:
            got_action = "fix"
            print(f"  {'LLM fix agent' if fixagent else 'fast path'} opened PR #{pr['pr']} ({pr['playbook']}): {pr['url']}", flush=True)
            try:
                merged = approve_pr(pr, fstate, approve)
            except Exception as ex:
                merged = False; rec["notes"].append(f"approval failed: {ex}")
            if merged:
                ttr_wait = wait_for(lambda: all_healthy(apps_to_check), RECOVERY_TIMEOUT)
                recovered_by_fix = ttr_wait is not None
                ttr = round(time.time() - t_push, 1) if recovered_by_fix else None
            else:
                recovered_by_fix = False
        elif scn["expected"]["action"] == "fix":
            got_action = "escalate"
        rec["responder"] = {"action": got_action, "via": "fixagent" if fixagent else "fastpath"}
    run(["git", "fetch", "-q", "origin", "main"])
    others = int(run(["git", "rev-list", "--count", f"{sha}..origin/main"]).stdout.strip())
    fix_commits = others > 0

    # revert (skip if a responder's fix already restored the healthy content)
    revert_ok = False
    try:
        run(["git", "pull", "-q", "--ff-only", "origin", "main"])
        if healthy_file.read_text(encoding="utf-8") == original:
            rec["notes"].append("healthy overlay already restored by a fix; revert skipped")
        else:
            run(["git", "revert", "--no-edit", sha])
            run(["git", "push", "-q", "origin", "main"])
        t_rev = time.time()
        r = wait_for(lambda: all_healthy(apps_to_check), RECOVERY_TIMEOUT)
        revert_ok = r is not None
        rec["revert_recovery_s"] = round(time.time() - t_rev, 1) if revert_ok else None
    except Exception as ex:
        run(["git", "revert", "--abort"], check=False)
        rec["notes"].append(f"REVERT FAILED, fix by hand: {ex}")
    if fp:
        if any(e["event"] == "pr_opened" for e in read_events(fp[1])):   # let it record the merge and tidy its branch
            wait_for(lambda: any(e["event"] in ("pr_merged", "pr_closed") for e in read_events(fp[1])), 40)
        stop_fastpath(fp)
        evs = [e for e in read_events(fp[1]) if e.get("ts", 0) >= t_push - 1]
        rec["fastpath"] = score_fastpath(scn, evs, t_push)
        if fixagent:
            rec["fixagent"] = True
            rec["fastpath"]["attempts"] = sum(1 for e in evs if e["event"] == "fix_attempt")
            rec["fastpath"]["gate_blocked"] = sum(1 for e in evs if e["event"] == "gate" and e.get("level") in ("blocked", "L0"))
        print(f"  fast path: {rec['fastpath']['prs']} PR(s) (expected {rec['fastpath']['expected']}) -> "
              f"{'ok' if rec['fastpath']['ok'] else 'WRONG'}", flush=True)
    if det:
        proc, log, err = det
        # let the incident(s) resolve so anything the recovery itself triggers is also counted
        wait_for(lambda: summarize(log, t_push - 1)[1] == 0, SETTLE_TIMEOUT)
        stop_detector(proc, err)
        incs, still_open = summarize(log, t_push - 1)
        incs = sorted(incs, key=lambda i: i["opened_at"])
        brief = [{"id": i["id"], "app": i["app"], "affected_apps": i["affected_apps"],
                  "opened_after_inject_s": round(i["opened_at"] - t_push, 1), "state": i["state"],
                  "signals": sorted({x["type"] for x in i["signals"]})} for i in incs]
        rec["detection"] = score_detection(scn, brief, still_open)
        rec["incidents"] = brief
        print(f"  incidents: {len(brief)} (expected {rec['detection']['expected']}) -> {'ok' if rec['detection']['ok'] else 'WRONG'}", flush=True)
    rec["inject_sha"] = sha
    rec["diagnosis_expected"] = scn["expected"]["diagnosis"]
    diag = score_diagnosis(scn["expected"]["diagnosis"], (got or {}).get("diagnosis")) if responder else None
    rec["score"] = score_scenario(scn, symptom_s, diag, got_action, fix_commits, recovered_by_fix, ttr, revert_ok)
    return rec


# --- output --------------------------------------------------------------------
def fmt(v): return "-" if v is None else (f"{v}" if not isinstance(v, bool) else ("yes" if v else "no"))


def print_summary(results, agg):
    print(f"\n{'scenario':<22}{'symptom':<9}{'detect_s':<10}{'incidents':<10}{'diagnosis':<10}{'action':<10}{'fixed':<7}{'false_fix':<10}{'revert_ok':<10}harness")
    for r in results:
        s = r["score"]; d = s["diagnosis"]
        print(f"{r['id']:<22}{fmt(s['symptom_observed']):<9}{fmt(r.get('detect_s')):<10}{fmt(None if not r.get('detection') else r['detection']['count']):<10}{fmt(None if d is None else d['correct']):<10}"
              f"{fmt(s['action']):<10}{fmt(s['fixed']):<7}{fmt(s['false_fix']):<10}{fmt(s['revert_recovered']):<10}{'OK' if s['harness_ok'] else 'FAIL'}")
    print("\naggregate:", json.dumps(agg))


def cmd_run(args):
    scns = load_scenarios()
    errs = validate(scns)
    if errs:
        print("scenario validation failed:\n  " + "\n  ".join(errs)); return 2
    ids = list(scns) if args.ids == ["all"] else args.ids
    if args.fixagent and args.fastpath: print("--fixagent and --fastpath are alternatives (the LLM fixer runs alone so it is measured on every scenario)"); return 2
    if args.fixagent: args.fastpath = True
    if args.fastpath: args.detector = True
    unknown = [i for i in ids if i not in scns]
    if unknown: print(f"unknown scenario(s): {', '.join(unknown)}"); return 2
    print(f"scenarios: {', '.join(ids)}\nresponder: {args.responder or 'none (harness check only)'}\ndetector: {'on' if args.detector else 'off'}\nfast path: {('on, approve=' + args.approve) if args.fastpath and not args.fixagent else 'off'}\nLLM fix agent: {('on, approve=' + args.approve) if args.fixagent else 'off'}")
    if args.dry_run:
        for i in ids:
            s = scns[i]
            print(f"- {i}: copy apps/{s['inject']['app']}/overlays/{s['inject']['overlay']} over its healthy overlay, commit + push to main; "
                  f"expect {s['observe']['symptom']['type']} on {s['observe']['app']}; then revert + push; recovery of {s['recovery']['apps']}")
        print("dry run: no Git or cluster changes made"); return 0
    err = precheck()
    if err: print(f"precheck failed: {err}"); return 2
    if args.fastpath:
        if args.responder: print("--fastpath and --responder are mutually exclusive"); return 2
        if not os.environ.get("FIXER_GITHUB_TOKEN"): print("--fastpath/--fixagent needs FIXER_GITHUB_TOKEN (see docs/phase4.md)"); return 2
        if args.fixagent and not os.environ.get("OPENAI_API_KEY"): print("--fixagent needs OPENAI_API_KEY"); return 2
    if not args.yes:
        print(f"\nThis makes {2 * len(ids)} commits (inject + revert per scenario) and PUSHES them to origin/main (public).")
        if args.fastpath: print("The fast path also opens PRs on GitHub; with --approve auto this runner merges them by pushing to main.")
        if input("Proceed? [y/N] ").strip().lower() != "y": print("aborted"); return 1
    results = []; code = 0
    for i in ids:
        print(f"\n== {i} ==", flush=True)
        try:
            rec = run_scenario(scns[i], args.responder, args.responder_timeout, args.detector, args.fastpath, args.approve, args.fixagent)
        except Exception as ex:
            print(f"  ERROR: {ex}\n  Check `git log` / `git status`; a revert may be needed.")
            return 3
        results.append(rec)
        if rec.get("aborted") or not rec["score"]["revert_recovered"]:
            print("  stopping: apps are not healthy; remaining scenarios skipped"); code = 3; break
    agg = aggregate(results)
    out = Path(args.out) if args.out else ROOT / "evals" / "results" / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"responder": args.responder, "results": results, "aggregate": agg}, indent=2), encoding="utf-8")
    print_summary(results, agg); print(f"\nresults: {out}")
    ok = (all(r["score"]["harness_ok"] for r in results) and all(r["detection"]["ok"] for r in results if r.get("detection"))
          and all(r["fastpath"]["ok"] and (r["expected_action"] == "none" or r["score"]["fixed"]) for r in results if r.get("fastpath")))
    return code or (0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list"); sub.add_parser("validate")
    r = sub.add_parser("run")
    r.add_argument("ids", nargs="+"); r.add_argument("--yes", action="store_true"); r.add_argument("--dry-run", action="store_true")
    r.add_argument("--responder"); r.add_argument("--detector", action="store_true"); r.add_argument("--fastpath", action="store_true"); r.add_argument("--fixagent", action="store_true"); r.add_argument("--approve", choices=["auto", "manual"], default="auto"); r.add_argument("--responder-timeout", type=int, default=600); r.add_argument("--out")
    s = sub.add_parser("summary"); s.add_argument("file")
    a = p.parse_args()
    if a.cmd == "list":
        for i, s in load_scenarios().items():
            print(f"{i:<22}{s['expected']['action']:<5} {s['expected']['diagnosis']['category']:<18}{s['description']}")
        return 0
    if a.cmd == "validate":
        errs = validate(load_scenarios())
        print("\n".join(errs) if errs else f"{len(load_scenarios())} scenarios valid"); return 1 if errs else 0
    if a.cmd == "summary":
        d = json.loads(Path(a.file).read_text(encoding="utf-8")); print_summary(d["results"], d["aggregate"]); return 0
    return cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
