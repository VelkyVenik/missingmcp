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


def test_worker_start_failure_maps_to_session_expired(tmp_path, fake_worker):
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
    assert r.status_code == 502
    assert r.json() == {                       # stable error shape, byte-for-byte
        "error": "garmin_session_expired",
        "message": "Your Garmin session expired. Please reconnect the Garmin MCP server.",
    }


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
