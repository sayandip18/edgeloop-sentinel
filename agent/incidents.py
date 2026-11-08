"""Incident manager: dedupes and correlates detector signals into one incident record per failure.

Rules
- One open incident per *root* app. A signal is routed to `caused_by` if the detector attributed it to a
  dependency, else to the app that shows it. So a web-app that is not Ready because backend-app is down lands
  in backend-app's incident (one failure, one incident), and its own app is listed under `affected_apps`.
- Repeated or escalating signals (OOMKilled, then CrashLoopBackOff, then restarts) update the same incident.
- An incident resolves after `resolve_after_s` seconds with no signals. A later failure opens a new incident.
- Every change is appended to a JSONL log (full incident snapshot per line), so state survives a restart and
  the eval harness can read what happened. The manager only reads signals; it has no cluster access.
"""
import json
import time
from pathlib import Path


class IncidentManager:
    def __init__(self, log_path=None, resolve_after_s=45.0):
        self.resolve_after_s = resolve_after_s
        self.log_path = Path(log_path) if log_path else None
        self.open = {}      # root app -> incident dict
        self._seq = 0
        if self.log_path and self.log_path.exists():
            self._replay()

    # --- persistence ---------------------------------------------------------
    def _replay(self):
        last = {}
        for ev in read_events(self.log_path):
            if "incident" in ev:
                last[ev["incident"]["id"]] = ev["incident"]
        self._seq = len(last)
        for inc in last.values():
            if inc["state"] == "open":
                self.open[inc["app"]] = inc

    def _emit(self, event, inc, now, out):
        ev = {"ts": now, "event": event, "incident": json.loads(json.dumps(inc))}
        out.append(ev)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")

    # --- core ----------------------------------------------------------------
    def observe(self, signals, now=None):
        """Feed one detection pass. Returns the list of events (opened / updated / resolved) it caused."""
        now = time.time() if now is None else now
        events = []
        routed = {}
        for s in signals:
            routed.setdefault(s.caused_by or s.app, []).append(s)

        for root, sigs in sorted(routed.items()):
            inc = self.open.get(root)
            created = inc is None
            if created:
                self._seq += 1
                inc = {"id": f"inc-{self._seq:04d}-{root}", "app": root, "state": "open", "opened_at": now,
                       "last_seen": now, "resolved_at": None, "affected_apps": [], "signals": []}
                self.open[root] = inc
            changed = created
            inc["last_seen"] = now
            for s in sigs:
                if s.app not in inc["affected_apps"] and s.app != root:
                    inc["affected_apps"].append(s.app); changed = True
                key = (s.type, s.app)
                rec = next((x for x in inc["signals"] if (x["type"], x["app"]) == key), None)
                if rec is None:
                    inc["signals"].append({"type": s.type, "app": s.app, "pod": s.pod, "detail": s.detail,
                                           "first_seen": now, "last_seen": now, "count": 1})
                    changed = True
                else:
                    rec["last_seen"] = now; rec["count"] += 1
                    if rec["detail"] != s.detail:
                        rec["detail"] = s.detail
            if changed:
                self._emit("opened" if created else "updated", inc, now, events)

        for root, inc in list(self.open.items()):
            if root not in routed and now - inc["last_seen"] >= self.resolve_after_s:
                inc["state"] = "resolved"; inc["resolved_at"] = now
                del self.open[root]
                self._emit("resolved", inc, now, events)
        return events


def read_events(path):
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass   # a half-written last line while the watcher is running
    return out


def summarize(path, since=0.0):
    """Final state of every incident opened at/after `since`, plus how many are still open."""
    last = {}
    for ev in read_events(path):
        if "incident" in ev:
            last[ev["incident"]["id"]] = ev["incident"]
    incs = [i for i in last.values() if i["opened_at"] >= since]
    return incs, sum(1 for i in incs if i["state"] == "open")
