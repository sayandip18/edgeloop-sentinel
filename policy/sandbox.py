"""Sandbox (Phase 5): reproduce-then-fix inside a throwaway vcluster. Used by the gate with --sandbox.

    reproduce_then_fix(before_yaml, after_yaml, app=...) -> {"reproduced": bool|None, "recovered": bool, "detail": [...]}

Steps: create a vcluster in its own host namespace, apply the CURRENT manifests (the failure should show: pods not
healthy), replace them with the PATCHED manifests, require every pod Ready and restart-free for a stable window,
then delete the vcluster and its host namespace (always, even on error).

Isolation, honestly:
  * The virtual cluster has its own API server and no copy of the host's Secrets or ServiceAccounts, so no real
    secrets are present. The sandbox only ever receives the app's built manifests.
  * Egress: a default-deny-egress NetworkPolicy is applied to the synced workload pods. Docker Desktop's CNI does NOT
    enforce NetworkPolicy, so there it is declared, not enforced (reported in the result). The k3s VM move fixes that.
  * Pods run on the host's node, so locally built images (imagePullPolicy: Never) are found.
  * It needs rights to create namespaces and install a Helm chart on the host cluster, so it runs under the
    operator's context (EDGELOOP_SANDBOX_CONTEXT, default docker-desktop), NOT the diagnoser/fixer/deployer.
"""
import json, os, secrets, shutil, subprocess, tempfile, time
from pathlib import Path

CTX = os.environ.get("EDGELOOP_SANDBOX_CONTEXT", "docker-desktop")
REPRO_WAIT = float(os.environ.get("SANDBOX_REPRO_WAIT", "40"))
HEAL_TIMEOUT = float(os.environ.get("SANDBOX_HEAL_TIMEOUT", "120"))
STABLE_S = float(os.environ.get("SANDBOX_STABLE_S", "20"))


def _tool(name):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from gate import find_tool
    return find_tool(name)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


class Sandbox:
    def __init__(self, runner=_run, name=None):
        self.run = runner
        self.name = name or f"sbx-{secrets.token_hex(3)}"
        self.kubectl, self.vcluster = _tool("kubectl"), _tool("vcluster")
        self.kc = None
        self.tmp = Path(tempfile.mkdtemp(prefix="sentinel-sbx-"))

    def host(self, *args):
        return self.run([self.kubectl, "--context", CTX, *args])

    def virt(self, *args, **kw):
        return self.run([self.kubectl, "--kubeconfig", str(self.kc), *args], **kw)

    def create(self):
        if not (self.kubectl and self.vcluster):
            raise RuntimeError("kubectl and vcluster are required (winget install loft-sh.vcluster)")
        r = self.run([self.vcluster, "create", self.name, "-n", self.name, "--connect=false", "--context", CTX])
        if r.returncode != 0:
            raise RuntimeError(f"vcluster create failed: {(r.stderr or r.stdout).strip()[-300:]}")
        r = self.run([self.vcluster, "connect", self.name, "-n", self.name, "--print", "--context", CTX])
        if r.returncode != 0 or "apiVersion" not in r.stdout:
            raise RuntimeError(f"vcluster connect failed: {(r.stderr or r.stdout).strip()[-300:]}")
        self.kc = self.tmp / "kubeconfig"
        self.kc.write_text(r.stdout, encoding="utf-8")
        # synced workload pods are labelled by vcluster; deny their egress (see module docstring)
        np = ("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: sandbox-deny-egress}\n"
              f"spec:\n  podSelector:\n    matchLabels: {{vcluster.loft.sh/managed-by: {self.name}}}\n  policyTypes: [Egress]\n  egress: []\n")
        f = self.tmp / "np.yaml"; f.write_text(np, encoding="utf-8")
        self.host("-n", self.name, "apply", "-f", str(f))

    def apply(self, yaml_text):
        f = self.tmp / "manifests.yaml"; f.write_text(yaml_text, encoding="utf-8")
        self.virt("create", "namespace", "apps")
        r = self.virt("-n", "apps", "apply", "-f", str(f))
        if r.returncode != 0:
            raise RuntimeError(f"apply failed: {r.stderr.strip()[-300:]}")

    def delete_manifests(self, yaml_text):
        f = self.tmp / "old.yaml"; f.write_text(yaml_text, encoding="utf-8")
        self.virt("-n", "apps", "delete", "-f", str(f), "--wait=true", "--ignore-not-found")

    def pods_healthy(self):
        r = self.virt("-n", "apps", "get", "pods", "-o", "json")
        if r.returncode != 0:
            return False
        items = json.loads(r.stdout or "{}").get("items", [])
        if not items:
            return False
        for p in items:
            cs = p.get("status", {}).get("containerStatuses") or []
            if p.get("status", {}).get("phase") != "Running" or not cs:
                return False
            if any((not c.get("ready")) or c.get("restartCount", 0) > 0 for c in cs):
                return False
        return True

    def wait(self, timeout, stable):
        """True once pods have been healthy continuously for `stable` seconds, before `timeout` elapses."""
        end, since = time.time() + timeout, None
        while time.time() < end:
            if self.pods_healthy():
                since = since or time.time()
                if time.time() - since >= stable:
                    return True
            else:
                since = None
            time.sleep(3)
        return False

    def destroy(self):
        if self.vcluster:
            self.run([self.vcluster, "delete", self.name, "-n", self.name, "--context", CTX])
        if self.kubectl:
            self.host("delete", "namespace", self.name, "--ignore-not-found", "--wait=false")
        shutil.rmtree(self.tmp, ignore_errors=True)


def reproduce_then_fix(before_yaml, after_yaml, app="app", sandbox=None):
    sb = sandbox or Sandbox()
    detail = [f"vcluster {sb.name} on context {CTX}",
              "egress: default-deny NetworkPolicy declared on synced pods (enforced only if the CNI supports it; Docker Desktop's does not)"]
    try:
        sb.create()
        sb.apply(before_yaml)
        reproduced = not sb.wait(REPRO_WAIT, STABLE_S)
        detail.append("current manifests: " + ("failure reproduced (pods not healthy)" if reproduced else "pods healthy, failure NOT reproduced"))
        sb.delete_manifests(before_yaml)
        sb.apply(after_yaml)
        recovered = sb.wait(HEAL_TIMEOUT, STABLE_S)
        detail.append("patched manifests: " + ("healthy and restart-free for the stable window" if recovered else "not healthy before the timeout"))
        return {"reproduced": reproduced, "recovered": recovered, "detail": detail}
    finally:
        sb.destroy()
