"""Rule-based local detector. Pure functions: raw Kubernetes JSON in, signals out. No cluster, no I/O.

A *signal* is one observed symptom of one app (e.g. "oom-app's container was OOMKilled"). Signals are not
incidents: the incident manager (incidents.py) dedupes and correlates them.

Deliberately NOT used: log text. Logs are untrusted input (README principle 6) and the decoy app emits scary
ERROR lines while healthy. Rules look only at pod status, deployment spec/status and kubelet events.
"""
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# Signal types
OOM_KILLED = "oom_killed"
CRASH_LOOP = "crash_loop"
REPEATED_RESTARTS = "repeated_restarts"
IMAGE_PULL_ERROR = "image_pull_error"
CONFIG_ERROR = "config_error"
LIVENESS_FAILED = "liveness_probe_failed"
NOT_READY = "not_ready"
UNSCHEDULABLE = "unschedulable"
DEPENDENCY_DOWN = "dependency_down"

IMAGE_REASONS = {"ErrImageNeverPull", "ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
CONFIG_REASONS = {"CreateContainerConfigError", "CreateContainerError"}


@dataclass
class Config:
    not_ready_grace_s: float = 45.0   # Running-but-not-Ready must persist this long (rollouts take a few seconds)
    recent_window_s: float = 600.0    # terminations / events older than this are history, not symptoms
    min_restarts: int = 2             # one restart is noise (node restarts); two is a pattern


@dataclass
class Signal:
    type: str
    app: str                  # the app that shows the symptom
    detail: str
    pod: str | None = None
    caused_by: str | None = None   # another app this symptom is attributed to (dependency correlation)
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _age(ts, now):
    return None if ts is None else (now - ts).total_seconds()


def pod_app(pod):
    labels = pod.get("metadata", {}).get("labels", {}) or {}
    return labels.get("app") or pod["metadata"]["name"]


def _ready_since(pod):
    for c in pod.get("status", {}).get("conditions", []) or []:
        if c["type"] == "Ready":
            return parse_ts(c.get("lastTransitionTime"))
    return parse_ts(pod.get("status", {}).get("startTime"))


def _last_termination(cs):
    """Most recent terminated state (current or last) of a container status, or None."""
    best = None
    for st in (cs.get("state", {}), cs.get("lastState", {})):
        t = st.get("terminated")
        if t:
            fin = parse_ts(t.get("finishedAt"))
            if best is None or (fin and (best[0] is None or fin > best[0])):
                best = (fin, t)
    return best


def pod_signals(pod, now, cfg):
    out = []
    meta, st = pod["metadata"], pod.get("status", {})
    if meta.get("deletionTimestamp"):
        return out   # terminating: replaced pod, not a symptom
    app, name = pod_app(pod), meta["name"]

    def add(type_, detail, **extra):
        out.append(Signal(type_, app, detail, pod=name, extra=extra))

    for cs in st.get("containerStatuses", []) or []:
        c = cs["name"]
        waiting = cs.get("state", {}).get("waiting") or {}
        reason = waiting.get("reason")
        if reason in IMAGE_REASONS:
            add(IMAGE_PULL_ERROR, f"container {c} cannot start: {reason}", container=c, reason=reason)
        elif reason == "CrashLoopBackOff":
            add(CRASH_LOOP, f"container {c} is in CrashLoopBackOff (restarts={cs.get('restartCount', 0)})", container=c)
        elif reason in CONFIG_REASONS:
            add(CONFIG_ERROR, f"container {c} cannot be created: {reason}", container=c, reason=reason)

        term = _last_termination(cs)
        recent = term is not None and (_age(term[0], now) is None or _age(term[0], now) <= cfg.recent_window_s)
        if term and recent:
            fin, t = term
            if t.get("reason") == "OOMKilled":
                add(OOM_KILLED, f"container {c} was OOMKilled (restarts={cs.get('restartCount', 0)})", container=c)
            elif t.get("exitCode", 0) != 0 and cs.get("restartCount", 0) >= cfg.min_restarts:
                add(REPEATED_RESTARTS,
                    f"container {c} restarted {cs.get('restartCount', 0)} times, last exit code {t.get('exitCode')}",
                    container=c, exit_code=t.get("exitCode"), restarts=cs.get("restartCount", 0))

    conds = {c["type"]: c for c in st.get("conditions", []) or []}
    if st.get("phase") == "Running" and conds.get("Ready", {}).get("status") == "False":
        age = _age(_ready_since(pod), now)
        if age is not None and age >= cfg.not_ready_grace_s:
            add(NOT_READY, f"pod Running but not Ready for {int(age)}s", seconds=int(age))
    sched = conds.get("PodScheduled", {})
    if st.get("phase") == "Pending" and sched.get("status") == "False":
        age = _age(parse_ts(sched.get("lastTransitionTime")), now)
        if age is not None and age >= cfg.not_ready_grace_s:
            add(UNSCHEDULABLE, f"pod unschedulable for {int(age)}s: {sched.get('message', '')}".strip(), seconds=int(age))
    return out


def event_signals(events, pods, now, cfg):
    """Liveness probe failures come from kubelet events (the pod status only shows the resulting restarts)."""
    pod_apps = {p["metadata"]["name"]: pod_app(p) for p in pods if not p["metadata"].get("deletionTimestamp")}
    seen, out = set(), []
    for e in events:
        io = e.get("involvedObject", {})
        if io.get("kind") != "Pod" or io.get("name") not in pod_apps:
            continue
        if e.get("reason") != "Unhealthy" or "liveness probe failed" not in e.get("message", "").lower():
            continue
        ts = parse_ts(e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp"))
        age = _age(ts, now)
        if age is not None and age > cfg.recent_window_s:
            continue
        if io["name"] in seen:
            continue
        seen.add(io["name"])
        out.append(Signal(LIVENESS_FAILED, pod_apps[io["name"]], e["message"].strip(), pod=io["name"],
                          extra={"count": e.get("count", 1)}))
    return out


_URL_HOST = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?(?:[^@/]*@)?([a-z0-9]([a-z0-9-]*[a-z0-9])?)(?:\.[a-z0-9.-]+)?(?::\d+)?(?:/.*)?$")


def dependencies(deploy):
    """Names of other workloads this deployment points at via literal env values (URLs or host[:port]).
    Convention used here: a Service has the same name as its Deployment, so the host's first label is the app."""
    deps = set()
    for c in deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
        for e in c.get("env", []) or []:
            v = e.get("value")
            if isinstance(v, str) and ("://" in v or re.search(r":\d+$", v)):
                m = _URL_HOST.match(v.strip())
                if m:
                    deps.add(m.group(1))
    return deps


def deployment_down(deploy):
    sp = deploy.get("spec", {}).get("replicas", 1)
    ready = deploy.get("status", {}).get("readyReplicas") or 0
    if sp == 0:
        return "scaled to 0 replicas"
    if ready == 0:
        return "no ready replicas"
    return None


def detect(snapshot, now=None, cfg=None):
    """snapshot: {"pods": [...], "deployments": [...], "events": [...]} (raw Kubernetes items).
    Returns a list of Signal. Symptoms of an app whose dependency is down carry caused_by=<dependency>."""
    cfg = cfg or Config()
    now = now or datetime.now(timezone.utc)
    pods = snapshot.get("pods", [])
    signals = []
    for p in pods:
        signals.extend(pod_signals(p, now, cfg))
    signals.extend(event_signals(snapshot.get("events", []), pods, now, cfg))

    # Dependency correlation: a symptomatic app that points at a workload which is itself down.
    deploys = {d["metadata"]["name"]: d for d in snapshot.get("deployments", [])}
    symptomatic = {s.app for s in signals}
    for app in sorted(symptomatic):
        d = deploys.get(app)
        if not d:
            continue
        for dep in sorted(dependencies(d) - {app}):
            dd = deploys.get(dep)
            why = dd and deployment_down(dd)
            if why:
                for s in signals:
                    if s.app == app and s.caused_by is None:
                        s.caused_by = dep
                if not any(s.type == DEPENDENCY_DOWN and s.app == dep for s in signals):
                    signals.append(Signal(DEPENDENCY_DOWN, dep, f"{dep} is {why}; {app} depends on it",
                                          extra={"dependents": []}))
                for s in signals:
                    if s.type == DEPENDENCY_DOWN and s.app == dep and app not in s.extra["dependents"]:
                        s.extra["dependents"].append(app)
    return signals
