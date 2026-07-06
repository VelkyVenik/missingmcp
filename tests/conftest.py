import json
import socket
import threading
import time
import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int) -> None:
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            return
        except OSError:
            time.sleep(0.02)


class _FakeHttpServer:
    """Shared scaffolding for the fake upstreams: single-thread HTTPServer on a
    free port with per-call recording. Subclasses provide the handler class via
    _handler() (its closure may reference self for calls/knobs)."""

    def __init__(self):
        self.port = _free_port()
        self.calls = []
        self._httpd = HTTPServer(("127.0.0.1", self.port), self._handler())
        # silence broken-pipe tracebacks when a test client times out and hangs up
        self._httpd.handle_error = lambda *a: None

    def _handler(self) -> type:
        raise NotImplementedError

    def start(self):
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._t.start()
        return self

    def stop(self):
        self._httpd.shutdown()


class FakeWorker(_FakeHttpServer):
    """A minimal HTTP server mimicking garmin-mcp's /healthz and /mcp."""

    def _handler(self) -> type:
        worker = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                if self.path == "/healthz":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
                else:
                    self.send_response(404); self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                worker.calls.append(("POST", self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","result":{}}')

        return H


@pytest.fixture
def fake_worker():
    w = FakeWorker().start()
    _wait_listening(w.port)
    yield w
    w.stop()


class FakeRemoteUpstream(_FakeHttpServer):
    """A minimal HTTP server mimicking a shared hosted MCP (remote-forward
    strategy A). Records every call; tests configure the reply:
      - response_status 200 + response_mode "json" (default): JSON body + Mcp-Session-Id
      - response_mode "sse": 200 text/event-stream body written in chunks
      - response_status 401/403/500/...: JSON error body, no session id
      - response_delay: seconds to sit on the request before answering (timeout tests)
      - response_json: overrides the 2xx payload (raw string; wrapped as the SSE
        data: line in "sse" mode)
    """

    def __init__(self):
        self.response_status = 200
        self.response_mode = "json"   # "json" | "sse"
        self.response_delay = 0.0
        self.response_json = None
        self.session_id = "up-sess-9"
        super().__init__()

    def _handler(self) -> type:
        upstream = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _handle(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                upstream.calls.append((self.command, self.path, dict(self.headers), body))
                if upstream.response_delay:
                    time.sleep(upstream.response_delay)
                self.send_response(upstream.response_status)
                payload = (upstream.response_json
                           or '{"jsonrpc":"2.0","result":{"remote":true}}').encode()
                if not 200 <= upstream.response_status < 300:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"upstream says no"}')
                elif upstream.response_mode == "sse":
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Mcp-Session-Id", upstream.session_id)
                    self.end_headers()
                    # two flushes so the body arrives in chunks, like a real SSE stream
                    self.wfile.write(b"event: message\n")
                    self.wfile.flush()
                    self.wfile.write(b"data: " + payload + b"\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", upstream.session_id)
                    self.end_headers()
                    self.wfile.write(payload)

            do_POST = _handle
            do_GET = _handle
            do_DELETE = _handle
            do_PUT = _handle

        return H


@pytest.fixture
def fake_remote():
    u = FakeRemoteUpstream().start()
    _wait_listening(u.port)
    yield u
    u.stop()


class StubRemoteAdapter:
    """A complete Adapter implementing the remote-forward strategy (A) against
    FakeRemoteUpstream. The strategy stays a first-class, tested part of the
    core even with no in-tree remote adapter (rohlik graduated to its official
    MCP); this stub pins the seam's contract for the next real one."""

    name = "acme"
    display_name = "Acme"
    authorize_template = "authorize.html"   # any real template; the stub reads its own fields
    second_factor_template = ""
    landing_template = "home.html"

    class _Forward:
        def __init__(self, upstream_url):
            self.upstream_url = upstream_url

        def headers(self, blob):
            import json
            d = json.loads(blob)
            return {"x-acme-user": d["user"], "x-acme-pass": d["pass"]}

    def __init__(self, upstream_url):
        self.forward = self._Forward(upstream_url)

    def login_hint(self, form):
        return form.get("acme_user", "")

    def start_login(self, form):
        import json
        from missingmcp.adapters.base import LoginError, LoginOk, normalize_account_key
        user, password = form.get("acme_user", ""), form.get("acme_pass", "")
        if "@" not in user:
            raise LoginError("Please enter a valid email address.", reason="auth")
        return LoginOk(account_key=normalize_account_key(user),
                       blob=json.dumps({"user": user, "pass": password}))

    def resume_second_factor(self, state, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("Acme sign-in does not use a verification code")

    def verify(self, blob):
        import json
        import httpx
        from missingmcp.adapters.base import LoginError
        try:
            r = httpx.post(self.forward.upstream_url, headers=self.forward.headers(blob),
                           content=b"{}", timeout=5.0)
        except httpx.HTTPError as e:
            raise LoginError("Acme could not be reached") from e
        if r.status_code in (401, 403):
            raise LoginError("Acme sign-in failed — check your credentials.", reason="auth")
        if r.status_code >= 400:
            raise LoginError("Acme could not be reached")
        return json.loads(blob)["user"]


class FakeWhoopUpstream(_FakeHttpServer):
    """WHOOP OAuth + v2 API fake. The token endpoint mints rotating at-<n>/rt-<n>
    pairs (each minted access token becomes valid); data endpoints require a
    valid Bearer. Knobs:
      - valid_tokens: access tokens the data endpoints accept (starts empty)
      - refresh_fails: refresh grant answers 400 invalid_grant
      - reject_data_auth: data endpoints answer 401 regardless of token
      - data_status: force this status from data endpoints (e.g. 429, 500)
      - profile: the /v2/user/profile/basic payload
    """

    def __init__(self):
        self.valid_tokens = set()
        self.refresh_fails = False
        self.reject_data_auth = False
        self.data_status = None
        self.mint = 0
        self.profile = {"user_id": 123, "email": "User@Example.com",
                        "first_name": "Test", "last_name": "User"}
        super().__init__()

    def _next_pair(self) -> dict:
        self.mint += 1
        at = f"at-{self.mint}"
        self.valid_tokens.add(at)
        return {"access_token": at, "refresh_token": f"rt-{self.mint}",
                "expires_in": 3600, "scope": "offline read:profile", "token_type": "bearer"}

    def _handler(self) -> type:
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send_json(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                up.calls.append(("POST", self.path, dict(self.headers), body))
                if self.path != "/oauth/oauth2/token":
                    return self._send_json(404, {"error": "not_found"})
                form = parse_qs(body.decode())
                if form.get("grant_type", [""])[0] == "refresh_token" and up.refresh_fails:
                    return self._send_json(400, {"error": "invalid_grant"})
                self._send_json(200, up._next_pair())

            def do_GET(self):
                up.calls.append(("GET", self.path, dict(self.headers), b""))
                if up.data_status:
                    return self._send_json(up.data_status, {"error": "forced"})
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if up.reject_data_auth or token not in up.valid_tokens:
                    return self._send_json(401, {"error": "unauthorized"})
                path = self.path.split("?")[0]
                if path == "/developer/v2/user/profile/basic":
                    return self._send_json(200, up.profile)
                if path == "/developer/v2/user/measurement/body":
                    return self._send_json(200, {"height_meter": 1.8,
                                                 "weight_kilogram": 80.0,
                                                 "max_heart_rate": 190})
                if path.startswith("/developer/v2/"):
                    # collections/by-id: echo the path so tests can assert routing
                    return self._send_json(200, {"records": [{"path": path}],
                                                 "next_token": None})
                self._send_json(404, {"error": "not_found"})

        return H


@pytest.fixture
def fake_whoop():
    u = FakeWhoopUpstream().start()
    _wait_listening(u.port)
    yield u
    u.stop()


class StubUpstreamOAuthAdapter:
    """A complete Adapter implementing the upstream-OAuth login shape (C) with
    a canned token exchange — pins oauth.authorize_get's redirect branch and
    oauth.authorize_callback without a real upstream. Form-login methods raise:
    app.py registers no authorize POST for upstream-OAuth adapters."""

    name = "acmeauth"
    display_name = "AcmeAuth"
    authorize_template = ""
    second_factor_template = ""
    landing_template = "home.html"

    def __init__(self, fail_with: str | None = None):
        self.fail_with = fail_with
        self.forward = None          # oauth-flow tests never touch the forward
        self.callbacks = []

    def authorize_redirect_url(self, state_id: str) -> str:
        return f"https://upstream.example/auth?state={state_id}"

    async def handle_callback(self, query):
        from missingmcp.adapters.base import LoginError, LoginOk, normalize_account_key
        self.callbacks.append(dict(query))
        if self.fail_with:
            raise LoginError(self.fail_with)
        return LoginOk(account_key=normalize_account_key("Me@X.cz"),
                       blob='{"access_token":"at","refresh_token":"rt","expires_at":9999999999}')

    def login_hint(self, form):
        return ""

    def start_login(self, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("AcmeAuth signs in at the provider, not here.")

    def resume_second_factor(self, state, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("AcmeAuth signs in at the provider, not here.")

    def verify(self, blob):
        return "Acme User"
