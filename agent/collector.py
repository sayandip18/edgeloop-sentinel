"""Read-only cluster collector. The ONLY thing in Phase 3 that talks to Kubernetes.

It shells out to kubectl with an explicit --kubeconfig for the `diagnoser` identity (never a kube context, so it
cannot silently fall back to the admin credentials) and, before the first read, proves that identity cannot
write: it refuses to run otherwise.
"""
import base64
import json
import re
import subprocess
import time

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

    def token_seconds_left(self):
        """Seconds until the kubeconfig's bearer token expires (negative = expired), or None if it cannot be read.
        Reads the file only; the token is never printed or sent anywhere."""
        try:
            m = re.search(r"^\s*token:\s*(\S+)", open(self.kubeconfig, encoding="utf-8").read(), re.M)
            payload = m.group(1).split(".")[1]
            exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
            return exp - time.time()
        except Exception:
            return None

    # --- Phase 6 reads (all within the diagnoser's RBAC: configmaps, pods/log in `apps`, GET via service proxy) ---
    def configmaps(self):
        r = self._kubectl("-n", self.ns, "get", "configmaps", "-o", "json")
        if r.returncode != 0:
            raise CollectorError(f"kubectl get configmaps failed: {(r.stderr or r.stdout).strip()[:200]}")
        return json.loads(r.stdout).get("items", [])

    def logs(self, pod, tail=80, previous=False, container=None):
        """Last `tail` log lines of a pod. Returns "" when there are none (e.g. no previous container)."""
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", pod or ""):
            raise CollectorError(f"refusing odd pod name {pod!r}")
        args = ["-n", self.ns, "logs", pod, f"--tail={max(1, min(int(tail), 300))}"]
        if previous:
            args.append("--previous")
        if container:
            args += ["-c", container]
        r = self._kubectl(*args)
        out = r.stdout if r.returncode == 0 else ""
        # Docker Desktop can answer --previous with an error string on stdout when the old log file is gone
        return "" if out.startswith("failed to try resolving symlinks") else out

    def raw_get(self, path):
        """GET one API-server path (used for the Prometheus/Loki service proxy; see obsquery.py)."""
        return self._kubectl("get", "--raw", path)
