from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from missingmcp import store, proxy, workers, security
from missingmcp.adapters.garmin import GarminAdapter, GarminWorkerForward
from missingmcp.config import load_config


def _cfg(tmp_path, fw):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
                        "DATA_DIR": str(tmp_path),
                        "WORKER_PORT_START": str(fw.port), "WORKER_PORT_END": str(fw.port)})


def _app(conn, mgr, cfg):
    rate = security.RateLimiter()
    adapter = GarminAdapter(cfg)
    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", adapter, conn, mgr, cfg,
                                      cfg.gateway_secret, rate)
    return TestClient(Starlette(routes=[Route("/mcp", mcp_post, methods=["POST"])]))


class FakeProc:
    def poll(self): return None
    def terminate(self): pass


def test_unauthorized_without_bearer(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0"})
    assert r.status_code == 401


def test_authorized_forwards_to_worker(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-123"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id") == "sess-1"
    assert fake_worker.calls and fake_worker.calls[-1][1] == "/mcp"


def test_bearer_for_other_adapter_is_rejected(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-other"
    # a token minted for a DIFFERENT adapter
    store.upsert_account(conn, "other", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "other", "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)                     # _app forwards to the GARMIN adapter
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401                  # garmin path must not accept a foreign token
    assert r.json() == {"error": "invalid_token"}   # authenticate() rejected it, not the unknown_account path


def test_worker_start_failure_maps_to_reauth_401(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-fail"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")

    def boom(*a):
        raise RuntimeError("no binary")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=boom)
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    # Dead upstream credentials must be a 401 with the RFC 9728 challenge, NOT a
    # 502 — a 502 is a dead end the MCP client retries forever; a 401 makes Claude
    # re-run authorization so the user self-heals with a fresh sign-in.
    assert r.status_code == 401
    assert r.json() == {                       # stable error shape, byte-for-byte
        "error": "invalid_token",
        # The one string an affected user reliably sees — it carries the full
        # recovery instruction, not just "reconnect" (reliability ticket 03).
        "message": "Your Garmin session expired. Please sign in to Garmin again "
                   "to reconnect — your MCP client will prompt you (in Claude: "
                   "Settings → Connectors → Garmin). Help: https://x/garmin",
    }
    wa = r.headers["www-authenticate"]
    assert wa.startswith("Bearer ")
    assert 'error="invalid_token"' in wa
    assert 'resource_metadata="https://x/.well-known/oauth-protected-resource/garmin/mcp"' in wa


def test_stale_credentials_worker_exit_maps_to_reauth_401(tmp_path, fake_worker):
    # The common case in production: the worker starts, rejects the stored tokens
    # and exits cleanly. It is logged apart from real worker faults
    # (`worker-forward-auth-stale` vs `worker-start-failed`) so the ops alert only
    # fires on the latter — but what the client sees must stay identical to any
    # other expired-session path: 401 + the RFC 9728 challenge.
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-stale"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")

    class SelfExitedProc:
        def poll(self): return 0
        def terminate(self): pass

    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: SelfExitedProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json() == {
        "error": "invalid_token",
        "message": "Your Garmin session expired. Please sign in to Garmin again "
                   "to reconnect — your MCP client will prompt you (in Claude: "
                   "Settings → Connectors → Garmin). Help: https://x/garmin",
    }
    assert 'resource_metadata="https://x/.well-known/oauth-protected-resource/garmin/mcp"' \
        in r.headers["www-authenticate"]


def test_unknown_account_maps_to_reauth_401(tmp_path, fake_worker):
    # Valid Bearer, but the account blob is gone (e.g. off-boarded upstream). Same
    # self-heal path: 401 + challenge so the client re-authorizes.
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-orphan"
    store.create_access_token(conn, store.hash_token(token), "garmin", "gone@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"
    assert 'resource_metadata="https://x/.well-known/oauth-protected-resource/garmin/mcp"' \
        in r.headers["www-authenticate"]


def test_authenticated_client_exceeds_unauth_limit(tmp_path, fake_worker):
    """A valid Bearer token is governed by the 60/min token bucket alone, NOT the
    stricter 30/min unauth-IP bucket — a data-heavy Claude session must not 429 at
    ~31 tool calls. (Finding #4: the unauth limit used to be consumed on every
    request, capping legitimate authenticated clients well below their token budget.)"""
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-heavy"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    headers = {"Authorization": f"Bearer {token}"}
    statuses = [c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
                       headers=headers).status_code for _ in range(40)]
    assert 429 not in statuses           # 40 < 60 token budget → never rate-limited
    assert all(s == 200 for s in statuses)


def test_unauth_flood_still_hits_unauth_limit(tmp_path, fake_worker):
    """Requests WITHOUT a valid Bearer token remain capped by the 30/min unauth-IP
    bucket: the 31st unauthenticated request in the window is 429, not 401."""
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    statuses = [c.post("/mcp", json={"jsonrpc": "2.0"}).status_code for _ in range(35)]
    assert statuses[:30] == [401] * 30   # first 30 unauthenticated → 401 unauthorized
    assert 429 in statuses[30:]          # once the unauth bucket is spent → 429


def test_invalid_token_flood_hits_unauth_limit(tmp_path, fake_worker):
    """An unknown Bearer token is still governed by the unauth-IP bucket, so
    token-guessing floods stay capped even though each guess gets its own tok
    bucket: the 31st bad-token request in the window is 429, not 401."""
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    statuses = [c.post("/mcp", json={"jsonrpc": "2.0"},
                       headers={"Authorization": f"Bearer guess-{i}"}).status_code
                for i in range(35)]
    assert statuses[:30] == [401] * 30   # first 30 bad tokens → 401 invalid_token
    assert 429 in statuses[30:]          # unauth bucket caps the guessing flood


# Remote-forward (strategy A) coverage lives in tests/test_remote_forward.py.


def test_mcp_tool_parsing():
    assert proxy._mcp_tool(b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_activities"},"id":1}') == "get_activities"
    assert proxy._mcp_tool(b'{"method":"tools/list","id":2}') == "tools/list"
    assert proxy._mcp_tool(b'{"method":"tools/call","params":{}}') == "tools/call"
    assert proxy._mcp_tool(b"") is None
    assert proxy._mcp_tool(b"not json") is None
    assert proxy._mcp_tool(b'[{"method":"x"}]') is None  # batch: skipped


def test_stream_teardown_is_a_warn_not_a_traceback(tmp_path, capsys):
    # A routine MCP session teardown: the worker aborts its open SSE stream
    # without the terminating chunk (httpx.RemoteProtocolError mid-stream).
    # That must NOT escape into the ASGI stack as an ERROR traceback — it was
    # ~80% of production error volume with zero demonstrated user impact
    # (reliability tickets 09/10). One structured warn event keeps it visible.
    import json as jsonlib
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class CutStream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence
            pass

        def do_GET(self):  # /healthz
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"event: message\n"
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            # hang up WITHOUT the terminating 0-length chunk — the teardown
            # signature. shutdown(), not just close(): rfile/wfile hold dup'd
            # FDs that would otherwise keep the TCP connection alive.
            import socket as socketlib
            self.connection.shutdown(socketlib.SHUT_RDWR)
            self.connection.close()

    httpd = HTTPServer(("127.0.0.1", 0), CutStream)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = store.init_db(":memory:")
        cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
                           "DATA_DIR": str(tmp_path),
                           "WORKER_PORT_START": str(port), "WORKER_PORT_END": str(port)})
        token = "tok-cut"
        store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
        store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")
        mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
        c = _app(conn, mgr, cfg)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.text == "event: message\n"       # the partial body still reaches the client
        events = [jsonlib.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        cut = next(e for e in events if e["event"] == "mcp-stream-interrupted")
        assert cut["level"] == "warn"              # visible to triage, invisible to the pager
        assert cut["error"] == "RemoteProtocolError"
        assert cut["bytes"] == len(b"event: message\n")
        resp = next(e for e in events if e["event"] == "mcp-response")
        assert resp["status"] == 200               # the finally bookkeeping still runs
    finally:
        httpd.shutdown()


def test_connect_error_is_retried_once_against_a_fresh_worker(tmp_path, fake_worker, capsys):
    # Ticket 14 belt-and-braces: a worker validated moments ago can still be
    # gone by the time the forward connects (stale-listener false-healthy).
    # One retry re-running ensure_worker must repair it invisibly: the client
    # gets 200, not 502.
    import json as jsonlib
    from conftest import _free_port

    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-retry"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())

    dead_port = _free_port()                       # nothing listening here
    ports = iter([dead_port, fake_worker.port])
    ensured = []

    async def fake_ensure(key, blob):
        p = next(ports)
        ensured.append(p)
        return p

    mgr.ensure_worker = fake_ensure
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert ensured == [dead_port, fake_worker.port]   # re-ensured exactly once
    events = [jsonlib.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert any(e.get("event") == "mcp-forward-retry" for e in events)


def test_connect_error_retry_gives_up_after_one_attempt(tmp_path, fake_worker, capsys):
    import json as jsonlib
    from conftest import _free_port

    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-giveup"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())

    dead1, dead2 = _free_port(), _free_port()
    ports = iter([dead1, dead2])
    ensured = []

    async def fake_ensure(key, blob):
        p = next(ports)
        ensured.append(p)
        return p

    mgr.ensure_worker = fake_ensure
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 502
    assert r.json() == {"error": "bad_gateway"}
    assert ensured == [dead1, dead2]               # exactly two attempts, no loop
    events = [jsonlib.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert any(e.get("event") == "mcp-forward-error" for e in events)
