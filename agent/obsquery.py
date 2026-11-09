"""Read-only query client for Prometheus and Loki (Phase 6). This is the "read-only query proxy" Phase 1 deferred.

Two layers keep it read-only:
1. RBAC (infra/rbac/diagnoser.yaml): the diagnoser may only `get` services/proxy for the two named services in
   `observability`. `get` through the API server's service proxy is an HTTP GET; nothing can be written.
2. This module: only the query endpoints below are reachable, only GET, only for queries scoped to the `apps`
   namespace, with length and result-size caps. Prometheus admin/config/targets and Loki push/delete paths are not
   in the allowlist, so they cannot be requested even though RBAC would let the GET through.

It shells out to kubectl with the diagnoser kubeconfig (like collector.py), so it never holds credentials itself.
"""
import json
import re
import time
import urllib.parse

PROM = "/api/v1/namespaces/observability/services/kps-prometheus:http-web/proxy"
LOKI = "/api/v1/namespaces/observability/services/loki:http-metrics/proxy"

ALLOWED = {
    "prometheus": (PROM, {"/api/v1/query", "/api/v1/query_range"}),
    "loki": (LOKI, {"/loki/api/v1/query_range", "/loki/api/v1/query"}),
}
MAX_QUERY_CHARS = 400
MAX_RANGE_S = 3600
MAX_OUT_CHARS = 6000
NS_SCOPE = re.compile(r'namespace\s*=~?\s*"apps"')


class QueryRefused(ValueError):
    pass


def validate(kind, endpoint, params):
    """Raises QueryRefused unless this is an allowed read-only query. Returns the API-server path to GET."""
    if kind not in ALLOWED:
        raise QueryRefused(f"unknown backend {kind!r}")
    base, allowed = ALLOWED[kind]
    if endpoint not in allowed:
        raise QueryRefused(f"endpoint {endpoint!r} is not an allowed query endpoint for {kind}")
    q = params.get("query", "")
    if not q or len(q) > MAX_QUERY_CHARS:
        raise QueryRefused(f"query must be 1-{MAX_QUERY_CHARS} characters")
    if not NS_SCOPE.search(q):
        raise QueryRefused('query must be scoped with namespace="apps"')
    clean = {"query": q}
    if "range" in endpoint:
        now = int(time.time())
        span = min(int(params.get("range_s", 900)), MAX_RANGE_S)
        clean.update({"start": now - span, "end": now})
        clean["step" if kind == "prometheus" else "limit"] = 30 if kind == "prometheus" else min(int(params.get("limit", 100)), 200)
    return f"{base}{endpoint}?{urllib.parse.urlencode(clean)}"


def query(collector, kind, endpoint, params):
    """Run one validated query through the diagnoser kubeconfig. Returns text (possibly truncated); raises QueryRefused
    for a disallowed query and RuntimeError when the backend is unreachable."""
    path = validate(kind, endpoint, params)
    r = collector.raw_get(path)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or "query failed")
    out = r.stdout
    try:
        out = json.dumps(json.loads(out)["data"], separators=(",", ":"))
    except Exception:
        pass
    return out if len(out) <= MAX_OUT_CHARS else out[:MAX_OUT_CHARS] + "...[truncated]"
