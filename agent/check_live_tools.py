#!/usr/bin/env python3
"""Live check of every agent tool against the real cluster (read-only, no LLM, no API key).

    python agent/check_live_tools.py [--kubeconfig kubeconfigs/diagnoser.kubeconfig] [--app healthy-app]

Exercises each tool through the same Toolbox the model uses, with the diagnoser identity, and checks that results land
in the evidence ledger and that disallowed calls come back as errors. Exit 0 = all good, 1 = a check failed,
2 = could not start (identity or credentials).
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from collector import Collector, CollectorError  # noqa: E402
from context import Evidence  # noqa: E402
from diagnose import Toolbox  # noqa: E402
from gitrepo import GitRepo  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubeconfig", default=str(ROOT / "kubeconfigs" / "diagnoser.kubeconfig"))
    ap.add_argument("--app", default="healthy-app")
    a = ap.parse_args()
    col = Collector(a.kubeconfig)
    try:
        col.assert_read_only()
        snap = col.snapshot()
    except CollectorError as e:
        print(e, file=sys.stderr); return 2
    repo = GitRepo(ROOT)
    try:
        repo.fetch("origin")
    except Exception as e:
        print(f"git fetch failed: {e}", file=sys.stderr); return 2
    ev = Evidence()
    tb = Toolbox(col, repo, ev, snap)
    pod = next((p["metadata"]["name"] for p in snap["pods"] if p["metadata"].get("labels", {}).get("app") == a.app), None)
    if pod is None:
        print(f"no pod for app {a.app!r} in the cluster", file=sys.stderr); return 2
    failures = []

    def good(name, **args):
        r = tb.call(name, args)
        ok = "evidence_id" in r and r["evidence_id"] in ev.ids() and "error" not in r
        print(f"  {'ok  ' if ok else 'FAIL'} {name}({', '.join(f'{k}={v!r}' for k, v in args.items())}) -> {r.get('evidence_id') or r.get('error')} "
              f"{(r.get('untrusted_data') or '')[:60]!r}")
        if not ok:
            failures.append(name)

    def refused(name, **args):
        r = tb.call(name, args)
        ok = "error" in r and "evidence_id" not in r
        print(f"  {'ok  ' if ok else 'FAIL'} refused {name}({', '.join(f'{k}={v!r}' for k, v in args.items())}) -> {r.get('error', 'ALLOWED')[:70]}")
        if not ok:
            failures.append(f"refused:{name}")

    good("get_logs", pod=pod, tail=20)
    good("get_events", app=a.app)
    good("get_recent_commits", app=a.app)
    good("get_live_vs_git", app=a.app)
    good("query_metrics", query='kube_pod_info{namespace="apps"}')
    good("query_metrics", query='kube_pod_container_status_restarts_total{namespace="apps"}', range_s=600)
    good("query_logs", query=f'{{namespace="apps", app="{a.app}"}}', range_s=300)
    refused("get_logs", pod="kube-apiserver-docker-desktop")
    refused("get_events", app="kube-system")
    refused("query_metrics", query="up")
    refused("query_logs", query='{namespace="kube-system"}')
    refused("delete_pod", pod=pod)
    print(f"{'all tool checks passed' if not failures else 'FAILED: ' + ', '.join(failures)}; evidence items: {len(ev.items)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
