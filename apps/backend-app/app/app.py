"""Tiny HTTP backend that web-app depends on. Answers 200 'ok' on every path."""
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, fmt, *a):
        print("backend request:", fmt % a, flush=True)

print("backend listening on 8080", flush=True)
HTTPServer(("", 8080), H).serve_forever()
