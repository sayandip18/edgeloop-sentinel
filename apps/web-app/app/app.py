"""Frontend that depends on a backend.
- Startup: BACKEND_URL must be set and its host must resolve, else exit 1 (wrong env var -> crash loop).
- /healthz: always 200 (liveness).
- /ready: 200 only if the backend answers (dependency outage -> Running but not Ready)."""
import os, socket, sys, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

url = os.environ.get("BACKEND_URL")
if not url:
    print("fatal: BACKEND_URL is not set", flush=True); sys.exit(1)
host = urlparse(url).hostname
try:
    socket.getaddrinfo(host, None)
except socket.gaierror as e:
    print(f"fatal: cannot resolve backend host {host!r}: {e}", flush=True); sys.exit(1)
print(f"web-app starting, backend={url}", flush=True)

last_log = 0.0
class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        global last_log
        if self.path == "/healthz":
            return self._send(200, b"ok")
        if self.path == "/ready":
            try:
                urllib.request.urlopen(url, timeout=1).read()
                return self._send(200, b"ready")
            except Exception as e:
                if time.time() - last_log > 10:
                    print(f"error: backend unreachable ({url}): {e}", flush=True); last_log = time.time()
                return self._send(503, b"backend unreachable")
        self._send(404, b"not found")
    def log_message(self, fmt, *a):
        pass

HTTPServer(("", 8080), H).serve_forever()
