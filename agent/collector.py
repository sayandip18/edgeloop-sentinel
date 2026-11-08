"""Read-only cluster collector. The ONLY thing in Phase 3 that talks to Kubernetes.

It shells out to kubectl with an explicit --kubeconfig for the `diagnoser` identity (never a kube context, so it
cannot silently fall back to the admin credentials) and, before the first read, proves that identity cannot
write: it refuses to run otherwise.
"""
import json
import subprocess

READ_OK = [("list", "pods"), ("list", "deployments.apps"), ("list", "events")]
MUST_BE_DENIED = [("create", "pods"), ("delete", "pods"), ("patch", "deployments.apps"), ("update", "configmaps"),
                  ("get", "secrets"), ("create", "pods/exec")]


class CollectorError(RuntimeError):
    def __init__(self, msg, fatal=False):
        super().__init__(msg)
        self.fatal = fatal   # fatal: retrying will not help (bad/expired credentials, wrong identity)


class Collector:
    def __init__(self, kubeconfig, namespace="apps"):
        self.kubeconfig, self.ns = str(kubeconfig), namespace

    def _kubectl(self, *args):
        try:
            return subprocess.run(["kubectl", "--kubeconfig", self.kubeconfig, *args], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise CollectorError("kubectl not found on PATH", fatal=True)
        except subprocess.TimeoutExpired:
            raise CollectorError("kubectl timed out")

    def assert_read_only(self):
        """Raise CollectorError(fatal) unless this kubeconfig can read the workload kinds and can write nothing."""
        for verb, res in READ_OK:
            r = self._kubectl("auth", "can-i", verb, res, "-n", self.ns)
            if r.stdout.strip() != "yes":
                err = (r.stderr or r.stdout).strip()
                raise CollectorError(f"identity cannot {verb} {res} in {self.ns} ({err}); is the token expired? "
                                     f"run: bash scripts/make-kubeconfig.sh diagnoser", fatal=True)
        for verb, res in MUST_BE_DENIED:
            r = self._kubectl("auth", "can-i", verb, res, "-n", self.ns)
            if r.stdout.strip() != "no":
                raise CollectorError(f"REFUSING TO RUN: this identity can {verb} {res}. The detector must use the "
                                     f"read-only diagnoser kubeconfig.", fatal=True)

    def snapshot(self):
        r = self._kubectl("-n", self.ns, "get", "pods,deployments.apps,events", "-o", "json")
        if r.returncode != 0:
            msg = r.stderr.strip() or r.stdout.strip()
            raise CollectorError(f"kubectl get failed: {msg}",
                                 fatal=any(w in msg for w in ("Unauthorized", "Forbidden", "forbidden", "unauthorized")))
        items = json.loads(r.stdout).get("items", [])
        snap = {"pods": [], "deployments": [], "events": []}
        for i in items:
            k = {"Pod": "pods", "Deployment": "deployments", "Event": "events"}.get(i.get("kind"))
            if k:
                snap[k].append(i)
        return snap
