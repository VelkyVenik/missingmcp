"""Park the Railway service during the hydra cutover (deploy/README.md).

Set as the service's Custom Start Command (`python3 scripts/railway_park.py`):
the redeploy SIGTERMs the gateway — which persists rotated tokens on the way
down — and this takes its place on the same /data volume without touching the
DB. It answers railway.json's /healthz check (so the deploy succeeds and the
container stays up for `railway ssh`) and 503s everything else.
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _reply(self) -> None:
        ok = self.path == "/healthz"
        body = b"parked\n" if ok else b"maintenance, back in a few minutes\n"
        self.send_response(200 if ok else 503)
        if not ok:
            self.send_header("Retry-After", "300")
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = do_HEAD = _reply

    def log_message(self, *args) -> None:
        # The base class writes access lines to stderr — Railway files those as errors.
        pass


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
