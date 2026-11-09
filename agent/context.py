"""Context builder (Phase 6): turns one incident into a bounded, redacted evidence bundle for the diagnosis agent.

Read-only by construction: it reads through a `Collector` (diagnoser kubeconfig, refuses write-capable identities) and
a read-only `GitRepo` view. It never writes anything and never calls an LLM.

Every piece of evidence gets a stable id (E1, E2, ...). The agent must cite those ids, and the validator rejects
citations that do not exist. ALL evidence text is untrusted: logs, event messages, config values and commit messages
can contain anything, including instructions aimed at a model. The text is redacted (redact.py), control characters
are stripped, and it is size-capped; the agent treats it strictly as data (see diagnose.py).
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from detector import dependencies, pod_app  # noqa: E402
from redact import redact  # noqa: E402

APP_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_TEXT = 3500
LOG_TAIL = 60


def clean_text(text, limit=MAX_TEXT):
    """Redact, strip control characters, cap length. Returns (text, redaction_counts)."""
    text = _CTRL.sub("", text or "")
    text, counts = redact(text)
    if len(text) > limit:
        text = text[:limit] + f"\n[... truncated, {len(text) - limit} more characters]"
    return text, counts


class Evidence:
    """Ordered, id-addressed evidence ledger. The same ledger is extended by the agent's tool calls."""

    def __init__(self):
        self.items, self.redactions = [], {}

    def add(self, kind, title, text, limit=MAX_TEXT):
        text, counts = clean_text(text, limit)
        for k, v in counts.items():
            self.redactions[k] = self.redactions.get(k, 0) + v
        item = {"id": f"E{len(self.items) + 1}", "kind": kind, "title": title, "text": text}
        self.items.append(item)
        return item

    def ids(self):
        return {i["id"] for i in self.items}


# --- summaries of raw Kubernetes objects ----------------------------------------------------
def summarize_pod(p):
    st, meta = p.get("status", {}), p["metadata"]
    conds = {c["type"]: c["status"] for c in st.get("conditions", []) or []}
    lines = [f"pod {meta['name']} phase={st.get('phase')} ready={conds.get('Ready')} scheduled={conds.get('PodScheduled')}"]
    for cs in st.get("containerStatuses", []) or []:
        cur = cs.get("state", {}); last = cs.get("lastState", {})
        lines.append(f"  container {cs['name']} image={cs.get('image')} ready={cs.get('ready')} restarts={cs.get('restartCount', 0)}")
        for label, s in (("state", cur), ("lastState", last)):
            for k in ("waiting", "terminated", "running"):
                if k in s:
                    v = s[k]
                    bits = {x: v[x] for x in ("reason", "exitCode", "message", "signal") if v.get(x) not in (None, "")}
                    lines.append(f"    {label}.{k} {bits if bits else ''}".rstrip())
    return "\n".join(lines)


def summarize_deployment(d):
    spec, st = d.get("spec", {}), d.get("status", {})
    lines = [f"deployment {d['metadata']['name']} replicas={spec.get('replicas')} ready={st.get('readyReplicas', 0)} "
             f"updated={st.get('updatedReplicas', 0)} generation={d['metadata'].get('generation')}"]
    for c in spec.get("template", {}).get("spec", {}).get("containers", []) or []:
        lines.append(f"  container {c['name']} image={c.get('image')}")
        for e in c.get("env", []) or []:
            lines.append(f"    env {e['name']}={e.get('value', '<from reference>')}")
        for ef in c.get("envFrom", []) or []:
            ref = (ef.get("configMapRef") or ef.get("secretRef") or {}).get("name")
            lines.append(f"    envFrom {'configMap' if 'configMapRef' in ef else 'secret'} {ref}")
        lines.append(f"    resources {c.get('resources', {})}")
        for pr in ("readinessProbe", "livenessProbe", "startupProbe"):
            if pr in c:
                lines.append(f"    {pr} {c[pr]}")
    return "\n".join(lines)


def configmaps_for(deploy, cms):
    names = set()
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
        for ef in c.get("envFrom", []) or []:
            if "configMapRef" in ef:
                names.add(ef["configMapRef"]["name"])
    return [c for c in cms if c["metadata"]["name"] in names]


# --- live vs Git ----------------------------------------------------------------------------
def git_manifests(repo, app, overlay="healthy"):
    """Build apps/<app>/overlays/<overlay> from the repo ref the way Flux does. Returns a list of parsed objects
    or raises RuntimeError."""
    if not APP_RE.match(app):
        raise RuntimeError(f"unsafe app name {app!r}")
    with tempfile.TemporaryDirectory(prefix="sentinel-ctx-") as d:
        repo.extract([f"apps/{app}"], d)
        target = Path(d) / "apps" / app / "overlays" / overlay
        r = subprocess.run(["kubectl", "kustomize", str(target)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:300] or "kustomize failed")
        return [o for o in yaml.safe_load_all(r.stdout) if o]


def _norm_container(c):
    return {"image": c.get("image"), "env": {e["name"]: e.get("value") for e in c.get("env", []) or []},
            "resources": c.get("resources", {}), "readinessProbe": c.get("readinessProbe"), "livenessProbe": c.get("livenessProbe")}


def drift(git_objs, live_deploy, live_cms):
    """Differences between what Git says should be deployed and what the cluster has. [] means in sync."""
    out = []
    gd = next((o for o in git_objs if o.get("kind") == "Deployment"), None)
    if gd is None:
        return ["Git has no Deployment for this app"]
    if live_deploy is None:
        return ["Git has a Deployment but the cluster does not"]
    if gd["spec"].get("replicas") != live_deploy["spec"].get("replicas"):
        out.append(f"replicas: git={gd['spec'].get('replicas')} live={live_deploy['spec'].get('replicas')}")
    gc = _norm_container(gd["spec"]["template"]["spec"]["containers"][0])
    lc = _norm_container(live_deploy["spec"]["template"]["spec"]["containers"][0])
    for k in ("image", "env", "resources", "readinessProbe", "livenessProbe"):
        if k == "env":
            for n in sorted(set(gc[k]) | set(lc[k])):
                if gc[k].get(n) != lc[k].get(n):
                    out.append(f"env {n}: git={gc[k].get(n)!r} live={lc[k].get(n)!r}")
        elif k in ("readinessProbe", "livenessProbe"):
            # the API server adds defaults to probes, so compare only keys Git sets
            for pk, pv in (gc[k] or {}).items():
                if (lc[k] or {}).get(pk) != pv:
                    out.append(f"{k}.{pk}: git={pv!r} live={(lc[k] or {}).get(pk)!r}")
        elif k == "resources":
            for sect in ("requests", "limits"):
                if (gc[k].get(sect) or {}) != (lc[k].get(sect) or {}):
                    out.append(f"resources.{sect}: git={gc[k].get(sect)} live={lc[k].get(sect)}")
        elif gc[k] != lc[k]:
            out.append(f"{k}: git={gc[k]!r} live={lc[k]!r}")
    live_by = {c["metadata"]["name"]: c.get("data", {}) or {} for c in live_cms}
    for o in git_objs:
        if o.get("kind") == "ConfigMap":
            n = o["metadata"]["name"]
            gdta, ldta = o.get("data", {}) or {}, live_by.get(n)
            if ldta is None:
                out.append(f"ConfigMap {n}: in git, missing in cluster")
                continue
            for key in sorted(set(gdta) | set(ldta)):
                if gdta.get(key) != ldta.get(key):
                    out.append(f"ConfigMap {n} key {key}: git={gdta.get(key)!r} live={ldta.get(key)!r}")
    return out


def manifest_specs(app):
    """Pathspecs for an app's deployable manifests (not its source code): what Flux applies."""
    return [f"apps/{app}/base/", f"apps/{app}/overlays/"]


# --- the builder ----------------------------------------------------------------------------
class ContextBuilder:
    def __init__(self, collector, repo=None, max_log_pods=2):
        self.col, self.repo, self.max_log_pods = collector, repo, max_log_pods

    def related_apps(self, incident, snapshot):
        apps = [incident["app"], *incident.get("affected_apps", [])]
        deploys = {d["metadata"]["name"]: d for d in snapshot["deployments"]}
        for a in list(apps):
            for dep in sorted(dependencies(deploys.get(a, {}))):
                if dep in deploys and dep not in apps:
                    apps.append(dep)
        return [a for a in apps if APP_RE.match(a)]

    def build(self, incident, focus_app=None):
        """incident: an incident record (incidents.py) or None. focus_app: app under review when there is no incident."""
        ev = Evidence()
        snap = self.col.snapshot()
        if incident is None:
            incident = {"id": None, "app": focus_app, "affected_apps": [], "signals": [], "state": "none"}
        apps = self.related_apps(incident, snap)
        known = {d["metadata"]["name"] for d in snap["deployments"]} | {pod_app(p) for p in snap["pods"]}
        cms = self.col.configmaps()

        sigs = "\n".join(f"- {s['type']} app={s['app']} pod={s.get('pod')} x{s.get('count', 1)}: {s['detail']}" for s in incident["signals"])
        ev.add("signals", "Detector signals (rule-based; may be empty)", sigs or "(none: the detector raised no signal)")

        for app in apps:
            pods = [p for p in snap["pods"] if pod_app(p) == app and not p["metadata"].get("deletionTimestamp")]
            dep = next((d for d in snap["deployments"] if d["metadata"]["name"] == app), None)
            ev.add("pods", f"Pods of {app}", "\n".join(summarize_pod(p) for p in pods) or "(no pods)")
            ev.add("deployment", f"Live Deployment {app}", summarize_deployment(dep) if dep else "(no Deployment)")
            for cm in configmaps_for(dep or {}, cms):
                ev.add("configmap", f"Live ConfigMap {cm['metadata']['name']} (used by {app})",
                       "\n".join(f"{k}={v}" for k, v in sorted((cm.get("data") or {}).items())) or "(no data keys)")
            names = {p["metadata"]["name"] for p in pods}
            evs = sorted((e for e in snap["events"] if e.get("involvedObject", {}).get("name") in names),
                         key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")[-15:]
            ev.add("events", f"Kubernetes events for {app}",
                   "\n".join(f"{e.get('type')} {e.get('reason')} x{e.get('count', 1)}: {e.get('message', '')}" for e in evs) or "(no events)")
            for p in pods[:self.max_log_pods]:
                n = p["metadata"]["name"]
                ev.add("log", f"Current container log of {n} (last {LOG_TAIL} lines)", self.col.logs(n, LOG_TAIL) or "(empty)")
                prev = self.col.logs(n, LOG_TAIL, previous=True)
                if prev:
                    ev.add("log", f"Previous (crashed) container log of {n} (last {LOG_TAIL} lines)", prev)
            if self.repo is not None:
                self._git_evidence(ev, app, dep, cms)
        return {"incident": {k: incident.get(k) for k in ("id", "app", "affected_apps", "state")},
                "apps_in_scope": apps, "known_apps": sorted(known), "evidence": ev.items}, ev

    def _git_evidence(self, ev, app, live_dep, cms):
        try:
            objs = git_manifests(self.repo, app)
            d = drift(objs, live_dep, configmaps_for(live_dep or {}, cms))
            ev.add("git-drift", f"Live cluster vs Git (main) for {app}",
                   "\n".join(d) if d else "In sync: the cluster matches what Git (main) says should be deployed.")
        except Exception as e:  # git or kustomize unavailable: say so rather than hide it
            ev.add("git-drift", f"Live cluster vs Git (main) for {app}", f"(unavailable: {e})")
        try:
            commits = self.repo.log(f"apps/{app}/", 4)
            parts = [f"commit {c.sha[:10]} by {c.author}: {c.subject}\n{self.repo.diff(c.sha, f'apps/{app}/', 1500)}" for c in commits]
            ev.add("git-commits", f"Recent Git commits touching apps/{app}/ (newest first)", "\n---\n".join(parts) or "(no commits)", limit=5000)
        except Exception as e:
            ev.add("git-commits", f"Recent Git commits touching apps/{app}/", f"(unavailable: {e})")
