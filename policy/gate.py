#!/usr/bin/env python3
"""Validation gate (Phase 5): an independent verdict on a proposed change. It writes nothing but a report.

    python policy/gate.py check --base-ref REF --head REF            # a commit range already in the local repo
    python policy/gate.py check --base-ref REF --changes-json FILE   # {"path": "new content" | null}; "-" = stdin
    options: --json  --sandbox  --allow-incomplete  --rules PATH  --repo DIR

Independence (principle 5): this file imports nothing from agent/ or playbooks/. The author of a patch hands over
file contents and a base ref; the gate rebuilds both worlds itself and judges the difference. Checks:

  scope         only apps/<app>/{base,overlays}/*.yaml, one app, small diff
  blast radius  no RBAC / NetworkPolicy / PV / PVC / Namespace / Secret ..., checked by file name, by file
                content and on the built output; no remote or cross-app kustomize references
  kustomize     the app builds exactly the way Flux builds it
  schema        kubeconform on the built output
  lint          kube-linter on before and after: only NEW findings fail
  policy        pod-security and resource rules from policy/rules.yaml on the built output
  sandbox       (--sandbox) reproduce the failure in a vcluster, apply the patch, confirm recovery

Exit codes: 0 pass, 1 blocked, 3 incomplete (a required tool is missing; treated as blocked by callers), 2 usage.
"""
import argparse, io, json, os, re, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required (pip install pyyaml)")

PASS, FAIL, SKIP = "pass", "fail", "skip"


# --- small utilities ----------------------------------------------------------------------------------------
def load_rules(path=None):
    return yaml.safe_load(Path(path or HERE / "rules.yaml").read_text(encoding="utf-8"))


def find_tool(name):
    """PATH first, then where winget drops portable tools, so a fresh install works without a new shell."""
    p = shutil.which(name)
    if p:
        return p
    base = os.environ.get("LOCALAPPDATA")
    if base:
        for d in (Path(base) / "Microsoft" / "WinGet" / "Links", *(Path(base) / "Microsoft" / "WinGet" / "Packages").glob("*")):
            for ext in (".exe", ""):
                c = d / (name + ext)
                if c.is_file():
                    return str(c)
    return None


def kustomize_cmd():
    k = find_tool("kustomize")
    if k:
        return [k, "build"]
    kc = find_tool("kubectl")
    return [kc, "kustomize"] if kc else None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def extract(repo, ref, paths, dest):
    data = git(repo, "archive", "--format=tar", ref, *paths)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest, filter="data")


def mem_mi(v):
    """Kubernetes memory quantity -> MiB (float), or None if unparseable."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]i?|m)?", str(v).strip())
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2) or ""
    f = {"": 1 / 1048576, "K": 1000 / 1048576, "Ki": 1 / 1024, "M": 1e6 / 1048576, "Mi": 1, "G": 1e9 / 1048576, "Gi": 1024,
         "T": 1e12 / 1048576, "Ti": 1048576, "m": 0.001 / 1048576}[u]
    return n * f


def cpu_m(v):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m)?", str(v).strip())
    if not m:
        return None
    return float(m.group(1)) * (1 if m.group(2) else 1000)


class Check:
    def __init__(self, name, status=PASS, detail=None, required=True):
        self.name, self.status, self.detail, self.required = name, status, list(detail or []), required

    def fail(self, msg):
        self.status = FAIL
        self.detail.append(msg)

    def as_dict(self):
        return {"name": self.name, "status": self.status, "detail": self.detail}


# --- change set -------------------------------------------------------------------------------------------------
def clean_path(p):
    if not isinstance(p, str) or not p or p.startswith(("/", "\\")) or "\\" in p or ".." in Path(p).parts or ":" in p:
        raise ValueError(f"unsafe path {p!r}")
    return p


def changes_from_range(repo, base, head):
    """{path: new content or None} for the files that differ between two refs. Binary files are reported as 'binary'."""
    out = {}
    for line in git(repo, "diff", "--name-status", "--no-renames", base, head).decode("utf-8").splitlines():
        st, path = line.split("\t", 1)
        if st == "D":
            out[path] = None
        else:
            blob = git(repo, "show", f"{head}:{path}")
            try:
                out[path] = blob.decode("utf-8")
            except UnicodeDecodeError:
                out[path] = "\x00binary"
    return out


def line_delta(old, new):
    import difflib
    a, b = (old or "").splitlines(), (new or "").splitlines()
    return sum(1 for l in difflib.ndiff(a, b) if l[:1] in "+-")


# --- checks --------------------------------------------------------------------------------------------------------
def check_scope(changes, old_text, rules):
    c = Check("scope")
    s = rules["scope"]
    rx = re.compile(s["allowed_path_regex"])
    apps = set()
    if not changes:
        c.fail("empty change set")
    for p in changes:
        m = rx.match(p)
        if not m:
            c.fail(f"{p}: outside the allowed scope (only apps/<app>/base|overlays/*.yaml may change automatically)")
        else:
            apps.add(m.group("app"))
    if len(apps) > s["max_apps"]:
        c.fail(f"touches {len(apps)} apps ({', '.join(sorted(apps))}); limit {s['max_apps']}")
    if len(changes) > s["max_files"]:
        c.fail(f"{len(changes)} files changed; limit {s['max_files']}")
    lines = sum(line_delta(old_text(p), n) for p, n in changes.items())
    if lines > s["max_changed_lines"]:
        c.fail(f"{lines} changed lines; limit {s['max_changed_lines']}")
    c.detail.append(f"{len(changes)} file(s), {lines} changed line(s), app(s): {', '.join(sorted(apps)) or '-'}")
    return c, sorted(apps), lines


def parse_docs(text):
    try:
        return [d for d in yaml.safe_load_all(text) if d is not None], None
    except yaml.YAMLError as e:
        return [], str(e).splitlines()[0]


def check_blast_radius(changes, built_before, built_after, rules):
    c = Check("blast-radius")
    b = rules["blast_radius"]
    fname = re.compile(b["denied_filename_regex"])
    denied = set(b["denied_kinds"])
    for p, text in changes.items():
        name = Path(p).name
        if fname.search(name):
            c.fail(f"{p}: file name suggests RBAC/network/storage/secret/namespace content")
        if text is None:
            continue
        docs, err = parse_docs(text)
        if err:
            c.fail(f"{p}: not valid YAML ({err})")
        for d in docs:
            if isinstance(d, dict) and d.get("kind") in denied:
                c.fail(f"{p}: contains a {d['kind']}")
            # kustomization references must stay inside the app and local
            if isinstance(d, dict) and d.get("kind") == "Kustomization" and str(d.get("apiVersion", "")).startswith("kustomize.config"):
                for key in ("resources", "bases", "components", "configurations", "crds"):
                    for ref in d.get(key) or []:
                        r = str(ref)
                        if re.search(r"://|^git@|^github\.com|\.git(/|$)|^git::", r):
                            c.fail(f"{p}: remote kustomize reference {r!r}")
                        else:
                            norm = os.path.normpath(os.path.join(os.path.dirname(p), r)).replace("\\", "/")
                            app = "/".join(p.split("/")[:2])
                            if not (norm + "/").startswith(app + "/"):
                                c.fail(f"{p}: reference {r!r} leaves {app}/")
                if d.get("namespace"):
                    c.fail(f"{p}: sets a kustomize namespace ({d['namespace']!r}); the namespace comes from Flux")
    for d in built_after:
        k, ns = d.get("kind"), (d.get("metadata") or {}).get("namespace")
        if k in denied:
            c.fail(f"built output contains {k}/{(d.get('metadata') or {}).get('name')}")
        if ns and ns not in b["allowed_namespaces"]:
            c.fail(f"{k}/{(d.get('metadata') or {}).get('name')} is in namespace {ns!r}; allowed: {b['allowed_namespaces']}")
    for d in built_after:
        if d.get("kind") not in rules["kinds"]["allowed"] and d.get("kind") not in denied:
            c.fail(f"kind {d.get('kind')} is not on the allowed list {rules['kinds']['allowed']}")
    return c


def pod_specs(docs):
    for d in docs:
        k, spec = d.get("kind"), d.get("spec") or {}
        if k in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
            yield d, spec.get("template", {}).get("spec") or {}
        elif k == "CronJob":
            yield d, (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
        elif k == "Pod":
            yield d, spec


def check_policy(built_after, rules):
    c = Check("policy")
    w = rules["workload"]
    img_rx = re.compile(w["image_allow_regex"])
    n = 0
    for d, ps in pod_specs(built_after):
        who = f"{d['kind']}/{(d.get('metadata') or {}).get('name')}"
        n += 1
        reps = (d.get("spec") or {}).get("replicas", 1)
        if isinstance(reps, int) and reps > w["max_replicas"]:
            c.fail(f"{who}: replicas {reps} > {w['max_replicas']}")
        if w["forbid_host_namespaces"] and any(ps.get(k) for k in ("hostNetwork", "hostPID", "hostIPC")):
            c.fail(f"{who}: uses a host namespace (hostNetwork/hostPID/hostIPC)")
        if w["forbid_host_path"] and any("hostPath" in v for v in ps.get("volumes") or []):
            c.fail(f"{who}: mounts a hostPath volume")
        pod_sc = ps.get("securityContext") or {}
        for ct in (ps.get("containers") or []) + (ps.get("initContainers") or []):
            cn = f"{who}[{ct.get('name')}]"
            image = str(ct.get("image", ""))
            if not img_rx.match(image):
                c.fail(f"{cn}: image {image!r} does not match {w['image_allow_regex']}")
            sc = ct.get("securityContext") or {}
            if w["forbid_privileged"] and sc.get("privileged"):
                c.fail(f"{cn}: privileged")
            if w["require_no_privilege_escalation"] and sc.get("allowPrivilegeEscalation") is not False:
                c.fail(f"{cn}: allowPrivilegeEscalation is not false")
            if w["require_run_as_non_root"] and not (sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot")) is True):
                c.fail(f"{cn}: runAsNonRoot is not true")
            if w["require_read_only_root_fs"] and sc.get("readOnlyRootFilesystem") is not True:
                c.fail(f"{cn}: readOnlyRootFilesystem is not true")
            if w["forbid_added_capabilities"] and (sc.get("capabilities") or {}).get("add"):
                c.fail(f"{cn}: adds capabilities {sc['capabilities']['add']}")
            res = ct.get("resources") or {}
            lim, req = res.get("limits") or {}, res.get("requests") or {}
            if w["require_memory_limit"] and "memory" not in lim:
                c.fail(f"{cn}: no memory limit")
            if w["require_cpu_limit"] and "cpu" not in lim:
                c.fail(f"{cn}: no cpu limit")
            lm = mem_mi(lim["memory"]) if "memory" in lim else None
            if "memory" in lim and lm is None:
                c.fail(f"{cn}: unreadable memory limit {lim['memory']!r}")
            if lm is not None and lm > w["max_memory_mi"]:
                c.fail(f"{cn}: memory limit {lim['memory']} exceeds {w['max_memory_mi']}Mi")
            rm = mem_mi(req["memory"]) if "memory" in req else None
            if lm is not None and rm is not None and rm > lm:
                c.fail(f"{cn}: memory request {req['memory']} is above its limit {lim['memory']}")
            lc = cpu_m(lim["cpu"]) if "cpu" in lim else None
            if lc is not None and lc > w["max_cpu_m"]:
                c.fail(f"{cn}: cpu limit {lim['cpu']} exceeds {w['max_cpu_m']}m")
    c.detail.append(f"{n} workload(s) checked")
    return c


# --- building ---------------------------------------------------------------------------------------------------------
def flux_target(tree, app):
    f = Path(tree) / "infra" / "flux" / "kustomizations.yaml"
    if not f.exists():
        return None
    for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
        if d and (d.get("metadata") or {}).get("name") == app:
            p = str((d.get("spec") or {}).get("path", ""))
            return p[2:] if p.startswith("./") else p
    return None


def build(tree, app):
    """Built manifests (text, docs) for the path Flux deploys for `app`, or (None, None, error)."""
    cmd = kustomize_cmd()
    if not cmd:
        return None, None, "no kustomize / kubectl on PATH"
    target = flux_target(tree, app)
    if not target or not target.startswith(f"apps/{app}/"):
        return None, None, f"no Flux Kustomization deploys apps/{app}/ (path={target!r})"
    r = run([*cmd, str(Path(tree) / target)])
    if r.returncode != 0:
        return None, None, f"kustomize build failed: {r.stderr.strip()[:300]}"
    docs, err = parse_docs(r.stdout)
    if err or not docs:
        return None, None, f"kustomize output unusable: {err or 'empty'}"
    return r.stdout, docs, None


def check_schema(text, rules):
    c = Check("schema")
    kc = find_tool("kubeconform")
    if not kc:
        c.status, c.detail = SKIP, ["kubeconform is not installed (winget install YannHamon.kubeconform)"]
        return c
    r = run([kc, *rules["tools"]["kubeconform_args"], "-"], input=text)
    if r.returncode != 0:
        for line in (r.stdout + r.stderr).strip().splitlines()[:6]:
            c.fail(line.strip())
    else:
        c.detail.append((r.stdout.strip().splitlines() or ["ok"])[-1])
    return c


def lint_findings(text, tmp):
    kl = find_tool("kube-linter")
    f = Path(tmp) / "lint-input.yaml"
    f.write_text(text, encoding="utf-8")
    r = run([kl, "lint", "--format", "json", str(f)])
    try:
        reports = json.loads(r.stdout or "{}").get("Reports") or []
    except json.JSONDecodeError:
        raise RuntimeError(f"kube-linter output not JSON: {(r.stdout + r.stderr)[:200]}")
    if r.returncode not in (0, 1):
        raise RuntimeError(f"kube-linter failed: {r.stderr.strip()[:200]}")
    out = {}
    for rep in reports:
        k8s = (rep.get("Object") or {}).get("K8sObject") or {}
        key = (rep.get("Check"), k8s.get("GroupVersionKind", {}).get("Kind"), k8s.get("Name"))
        out[key] = (rep.get("Diagnostic") or {}).get("Message", "")
    return out


def check_lint(before_text, after_text, tmp):
    c = Check("lint")
    if not find_tool("kube-linter"):
        c.status, c.detail = SKIP, ["kube-linter is not installed (winget install stackrox.kube-linter)"]
        return c
    try:
        before = lint_findings(before_text, tmp) if before_text else {}
        after = lint_findings(after_text, tmp)
    except RuntimeError as e:
        c.fail(str(e))
        return c
    new = [k for k in after if k not in before]
    for k in new:
        c.fail(f"new finding {k[0]} on {k[1]}/{k[2]}: {after[k][:160]}")
    c.detail.append(f"{len(after)} finding(s) after, {len(before)} before, {len(new)} new")
    return c


# --- orchestration ------------------------------------------------------------------------------------------------------
def evaluate(repo, base_ref, changes, rules, *, sandbox=False, sandbox_fn=None):
    """Judge `changes` (path -> new text or None) against the tree at base_ref. Returns the verdict dict."""
    checks, apps, lines = [], [], 0
    tmp = tempfile.mkdtemp(prefix="sentinel-gate-")
    try:
        try:
            changes = {clean_path(p): t for p, t in changes.items()}
        except ValueError as e:
            c = Check("scope"); c.fail(str(e))
            return verdict([c], [], 0)
        before_dir, after_dir = Path(tmp) / "before", Path(tmp) / "after"
        before_dir.mkdir(); after_dir.mkdir()
        extract(repo, base_ref, ["apps", "infra/flux/kustomizations.yaml"], before_dir)
        shutil.copytree(before_dir, after_dir, dirs_exist_ok=True)

        def old_text(p):
            f = before_dir / p
            return f.read_text(encoding="utf-8") if f.is_file() else None

        scope, apps, lines = check_scope(changes, old_text, rules)
        checks.append(scope)
        for p, text in changes.items():
            f = after_dir / p
            if text is None:
                f.unlink(missing_ok=True)
            elif "\x00" in text:
                scope.fail(f"{p}: binary content")
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8", newline="")
        if scope.status == FAIL and not apps:
            # nothing sensible to build; still run the content-level blast-radius check so the report is complete
            checks.append(check_blast_radius(changes, [], [], rules))
            return verdict(checks, apps, lines)
        before_text, before_docs, _ = build(before_dir, apps[0]) if apps else (None, [], None)
        after_text, after_docs, err = build(after_dir, apps[0]) if apps else (None, [], "no app")
        kb = Check("kustomize")
        if err:
            kb.fail(err)
        else:
            kb.detail.append(f"apps/{apps[0]} builds as Flux deploys it: {len(after_docs)} object(s)")
        checks.append(kb)
        checks.append(check_blast_radius(changes, before_docs or [], after_docs or [], rules))
        if err:
            return verdict(checks, apps, lines)
        checks.append(check_policy(after_docs, rules))
        checks.append(check_schema(after_text, rules))
        checks.append(check_lint(before_text, after_text, tmp))
        if sandbox:
            sc = Check("sandbox", required=True)
            try:
                if sandbox_fn is None:
                    sys.path.insert(0, str(HERE))
                    from sandbox import reproduce_then_fix as sandbox_fn
                res = sandbox_fn(before_text, after_text, app=apps[0])
                sc.detail.extend(res.get("detail", []))
                sc.status = PASS if res.get("recovered") else FAIL
                if not res.get("recovered"):
                    sc.detail.append("patched manifests did not become healthy in the sandbox")
                if res.get("reproduced") is False:
                    sc.detail.append("note: the failure did not reproduce in the sandbox (inconclusive reproduce step)")
            except Exception as e:  # a broken sandbox must never read as 'passed'
                sc.status = SKIP
                sc.detail.append(f"sandbox unavailable: {str(e)[:300]}")
            checks.append(sc)
        return verdict(checks, apps, lines)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verdict(checks, apps, lines):
    failed = [c.name for c in checks if c.status == FAIL]
    skipped = [c.name for c in checks if c.status == SKIP and c.required]
    status = "blocked" if failed else ("incomplete" if skipped else "pass")
    return {"status": status, "apps": apps, "changed_lines": lines, "failed": failed, "skipped": skipped,
            "checks": [c.as_dict() for c in checks]}


def render(v):
    lines = [f"gate: {v['status'].upper()}  (apps={','.join(v['apps']) or '-'}, {v['changed_lines']} changed lines)"]
    for c in v["checks"]:
        lines.append(f"  {c['status'].upper():<5} {c['name']}")
        for d in c["detail"]:
            lines.append(f"          {d}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base-ref", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--head")
    g.add_argument("--changes-json")
    c.add_argument("--repo", default=str(ROOT))
    c.add_argument("--rules")
    c.add_argument("--json", action="store_true")
    c.add_argument("--sandbox", action="store_true")
    c.add_argument("--allow-incomplete", action="store_true", help="exit 0 when only optional/required tools are missing (NOT for the fast path)")
    a = p.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        if a.head:
            changes = changes_from_range(a.repo, a.base_ref, a.head)
        else:
            raw = sys.stdin.read() if a.changes_json == "-" else Path(a.changes_json).read_text(encoding="utf-8")
            changes = json.loads(raw)
            if not isinstance(changes, dict) or not all(t is None or isinstance(t, str) for t in changes.values()):
                raise ValueError("changes must be a JSON object of path -> string|null")
        v = evaluate(a.repo, a.base_ref, changes, load_rules(a.rules), sandbox=a.sandbox)
    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as e:
        print(f"gate: cannot evaluate: {e}", file=sys.stderr)
        return 2
    print(json.dumps(v) if a.json else render(v))
    return {"pass": 0, "blocked": 1, "incomplete": 0 if a.allow_incomplete else 3}[v["status"]]


if __name__ == "__main__":
    sys.exit(main())
