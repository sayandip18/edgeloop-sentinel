"""Offline tests for the Phase 5 gate and risk engine. No cluster, no network.
Run: python policy/test_gate.py   (also collectable by pytest). Needs git and kubectl/kustomize; kubeconform and
kube-linter make the schema/lint cases real, otherwise those cases are reported as skipped.

Builds a throwaway git repo from the real apps/ tree and throws deliberately bad patches at the gate."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import gate, risk  # noqa: E402

RULES = gate.load_rules()
TOOLS = {t: bool(gate.find_tool(t)) for t in ("kubeconform", "kube-linter")}


def make_repo():
    d = Path(tempfile.mkdtemp(prefix="sentinel-gate-test-"))
    shutil.copytree(ROOT / "apps", d / "apps", ignore=shutil.ignore_patterns("app"))
    (d / "infra" / "flux").mkdir(parents=True)
    shutil.copy(ROOT / "infra" / "flux" / "kustomizations.yaml", d / "infra" / "flux" / "kustomizations.yaml")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "baseline"]):
        assert subprocess.run(["git", "-C", str(d), *args], capture_output=True, env=env).returncode == 0
    return d


REPO = make_repo()
DEP = "apps/oom-app/base/deployment.yaml"
OV = "apps/oom-app/overlays/healthy/kustomization.yaml"


def text(p):
    return (REPO / p).read_text(encoding="utf-8")


def judge(changes):
    return gate.evaluate(REPO, "main", changes, RULES)


def sub(p, old, new):
    t = text(p)
    assert old in t, (p, old)
    return {p: t.replace(old, new)}


def expect_blocked(name, changes, check):
    v = judge(changes)
    assert v["status"] == "blocked", f"{name}: expected blocked, got {v['status']}\n{gate.render(v)}"
    assert check in v["failed"], f"{name}: expected {check} to fail, got {v['failed']}\n{gate.render(v)}"


def expect_pass(name, changes):
    v = judge(changes)
    ok = v["status"] == "pass" or (v["status"] == "incomplete" and not v["failed"])
    assert ok, f"{name}: expected pass, got {v['status']}\n{gate.render(v)}"
    return v


# --- good patches pass ---------------------------------------------------------------------------------------------
def test_good_memory_bump_passes():
    expect_pass("memory 128->192", sub(DEP, "memory: 128Mi", "memory: 192Mi"))


def test_good_revert_of_injected_overlay_passes():
    broken = text("apps/oom-app/overlays/broken/kustomization.yaml")
    healthy = text(OV)
    # the healthy overlay was 'broken' by an injection; the fix restores it
    expect_pass("overlay restore", {OV: healthy})
    assert broken != healthy


# --- blast radius ------------------------------------------------------------------------------------------------------
def test_rbac_blocked():
    role = "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata: {name: x}\nrules: []\n"
    ov = text(OV) + "\n"
    c = {"apps/oom-app/base/role.yaml": role,
         "apps/oom-app/base/kustomization.yaml": text("apps/oom-app/base/kustomization.yaml") + "  - role.yaml\n"}
    expect_blocked("rbac", c, "blast-radius")


def test_networkpolicy_blocked():
    np = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: x}\nspec: {podSelector: {}}\n"
    expect_blocked("netpol", {"apps/oom-app/base/policy.yaml": np}, "blast-radius")


def test_pvc_blocked():
    pvc = "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: x}\nspec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}\n"
    expect_blocked("pvc", {"apps/oom-app/base/data.yaml": pvc}, "blast-radius")


def test_remote_and_cross_app_reference_blocked():
    k = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - ../../base\n  - https://github.com/x/y//z\n"
    expect_blocked("remote ref", {OV: k}, "blast-radius")
    k2 = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - ../../../web-app/base\n"
    expect_blocked("cross-app ref", {OV: k2}, "blast-radius")


def test_namespace_escape_blocked():
    k = text(OV) + "namespace: kube-system\n"
    expect_blocked("namespace", {OV: k}, "blast-radius")


# --- scope and size -----------------------------------------------------------------------------------------------------
def test_scope_violations_blocked():
    for p in ("infra/rbac/deployer.yaml", "agent/fastpath.py", "policy/rules.yaml", "apps/oom-app/app/app.py",
              "apps/oom-app/overlays/healthy/../../../web-app/base/deployment.yaml"):
        v = judge({p: "x: 1\n"})
        assert v["status"] == "blocked" and "scope" in v["failed"], p


def test_two_apps_blocked():
    c = {**sub(DEP, "memory: 128Mi", "memory: 192Mi"), **sub("apps/web-app/base/deployment.yaml", "memory: 64Mi", "memory: 96Mi")}
    expect_blocked("two apps", c, "scope")


def test_oversize_diff_blocked():
    big = text(DEP) + "".join(f"# filler line {i}\n" for i in range(60))
    expect_blocked("big diff", {DEP: big}, "scope")


# --- workload policy ------------------------------------------------------------------------------------------------------
def test_policy_violations_blocked():
    cases = {
        "privileged": sub(DEP, "allowPrivilegeEscalation: false", "allowPrivilegeEscalation: false\n            privileged: true"),
        "latest tag": sub(DEP, "edgeloop/oom-app:v1", "edgeloop/oom-app:latest"),
        "foreign registry": sub(DEP, "edgeloop/oom-app:v1", "evil.example/miner:v1"),
        "memory 4Gi": sub(DEP, "memory: 128Mi", "memory: 4Gi"),
        "no limits": sub(DEP, "limits: {cpu: 100m, memory: 128Mi}", "limits: {}"),
        "run as root": sub(DEP, "runAsNonRoot: true", "runAsNonRoot: false"),
        "escalation": sub(DEP, "allowPrivilegeEscalation: false", "allowPrivilegeEscalation: true"),
        "rw rootfs": sub(DEP, "readOnlyRootFilesystem: true", "readOnlyRootFilesystem: false"),
        "added cap": sub(DEP, "capabilities: {drop: [ALL]}", "capabilities: {drop: [ALL], add: [NET_ADMIN]}"),
        "10 replicas": sub(DEP, "replicas: 1", "replicas: 10"),
        "hostNetwork": sub(DEP, "    spec:\n      containers:", "    spec:\n      hostNetwork: true\n      containers:"),
        "hostPath": sub(DEP, "    spec:\n      containers:", "    spec:\n      volumes:\n        - {name: h, hostPath: {path: /}}\n      containers:"),
    }
    for name, c in cases.items():
        expect_blocked(name, c, "policy")


# --- build / schema / lint ---------------------------------------------------------------------------------------------------
def test_unbuildable_blocked():
    expect_blocked("missing resource", {OV: "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - ../../base\n  - nope.yaml\n"}, "kustomize")


def test_schema_violation_blocked():
    if not TOOLS["kubeconform"]:
        print("skip test_schema_violation_blocked (kubeconform missing)"); return
    expect_blocked("replicas as string", sub(DEP, "replicas: 1", "replicas: three"), "schema")


def test_new_lint_finding_blocked():
    if not TOOLS["kube-linter"]:
        print("skip test_new_lint_finding_blocked (kube-linter missing)"); return
    v = judge(sub(DEP, "requests: {cpu: 10m, memory: 32Mi}", "requests: {}"))
    assert "lint" in v["failed"], gate.render(v)
    assert v["status"] == "blocked", gate.render(v)


# --- risk engine ----------------------------------------------------------------------------------------------------------------
def test_risk_levels():
    auto = risk.load_autonomy()
    ok = {"status": "pass", "changed_lines": 2}
    d = risk.decide(ok, {"playbook": "revert-recent-change", "app": "oom-app", "files": {OV: "x"}, "old": {}}, auto)
    assert d["level"] == "L1", d
    assert risk.decide({"status": "blocked", "failed": ["policy"]}, {"playbook": "revert-recent-change"}, auto)["level"] == "blocked"
    assert risk.decide({"status": "incomplete", "skipped": ["schema"]}, {"playbook": "revert-recent-change"}, auto)["level"] == "blocked"
    assert risk.decide(ok, {"playbook": "unknown-class", "app": "oom-app", "files": {}}, auto)["level"] == "L0"
    big = {"status": "pass", "changed_lines": 40}
    assert risk.decide(big, {"playbook": "raise-memory-limit", "app": "web-app", "files": {"apps/web-app/base/d.yaml": "x"}, "old": {}}, auto)["level"] == "L0"
    hi = dict(auto, classes={"revert-recent-change": "L3"})
    assert risk.decide(ok, {"playbook": "revert-recent-change", "app": "oom-app", "files": {}}, hi)["level"] == "L1"


def test_cli_blocks_with_exit_1():
    p = subprocess.run([sys.executable, str(HERE / "gate.py"), "check", "--repo", str(REPO), "--base-ref", "main", "--changes-json", "-", "--json"],
                       input=json.dumps({"infra/rbac/deployer.yaml": "x: 1\n"}), capture_output=True, text=True)
    assert p.returncode == 1 and json.loads(p.stdout)["status"] == "blocked", (p.returncode, p.stdout, p.stderr)


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f(); print(f"ok {n}")
        except Exception as e:
            failed += 1; print(f"FAIL {n}: {e}")
    shutil.rmtree(REPO, ignore_errors=True)
    print(f"tools: {TOOLS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
