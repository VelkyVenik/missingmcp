"""Remote-forward (strategy A) behavior of the REAL RohlikAdapter through
proxy.handle_mcp, against the fake_remote upstream — mirrors how test_proxy.py
exercises the garmin worker path."""
import json
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, proxy, security
from garmin_gateway.adapters.rohlik import RohlikAdapter
from garmin_gateway.config import load_config

TOKEN = "tok-rohlik"
BLOB = json.dumps({"email": "me@x.cz", "password": "pw"})


def _cfg(upstream):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
                        "ROHLIK_MCP_URL": f"http://127.0.0.1:{upstream.port}/mcp"})


def _setup(upstream):
    """(conn, TestClient) with a rohlik account + Bearer token already stored.
    manager=None: the remote path must never need a WorkerManager."""
    conn = store.init_db(":memory:")
    cfg = _cfg(upstream)
    store.upsert_account(conn, "rohlik", "me@x.cz", BLOB, cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(TOKEN), "rohlik", "me@x.cz", "c1")
    adapter = RohlikAdapter(cfg)
    rate = security.RateLimiter()

    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", adapter, conn, None, cfg,
                                      cfg.gateway_secret, rate)
    client = TestClient(Starlette(routes=[Route("/rohlik/mcp", mcp_post, methods=["POST"])]))
    return conn, client


def _post(client, headers=None, body=None):
    return client.post("/rohlik/mcp",
                       json=body or {"jsonrpc": "2.0", "method": "initialize", "id": 1},
                       headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})})


def test_upstream_receives_injected_and_threaded_headers(fake_remote):
    _, c = _setup(fake_remote)
    r = _post(c, headers={"Accept": "application/json, text/event-stream",
                          "Mcp-Session-Id": "sess-abc"})
    assert r.status_code == 200
    method, path, hdrs, body = fake_remote.calls[-1]
    assert (method, path) == ("POST", "/mcp")
    assert hdrs.get("rhl-email") == "me@x.cz"        # injected from the decrypted blob
    assert hdrs.get("rhl-pass") == "pw"
    assert hdrs.get("Accept") == "application/json, text/event-stream"
    assert hdrs.get("Mcp-Session-Id") == "sess-abc"
    assert json.loads(body)["method"] == "initialize"


def test_json_response_passes_through(fake_remote):
    _, c = _setup(fake_remote)
    r = _post(c)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert r.headers.get("mcp-session-id") == "up-sess-9"
    assert r.json() == {"jsonrpc": "2.0", "result": {"remote": True}}


def test_sse_response_streams_through(fake_remote):
    fake_remote.response_mode = "sse"
    _, c = _setup(fake_remote)
    r = _post(c, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/event-stream"
    assert r.text == 'event: message\ndata: {"jsonrpc":"2.0","result":{"remote":true}}\n\n'


@pytest.mark.parametrize("status", [401, 403])
def test_upstream_auth_rejection_maps_to_session_expired(fake_remote, status):
    fake_remote.response_status = status
    _, c = _setup(fake_remote)
    r = _post(c)
    assert r.status_code == 502                      # not streamed through
    assert r.json() == {                             # stable error shape, byte-for-byte
        "error": "rohlik_session_expired",
        "message": "Your Rohlík session expired. Please reconnect the Rohlík MCP server.",
    }


def test_upstream_timeout_maps_to_504(fake_remote, monkeypatch):
    # pins the remote path's timeout exit (finish() is a no-op there — a revert
    # to unconditional manager.request_finished would crash on manager=None)
    fake_remote.response_delay = 0.5
    monkeypatch.setattr(proxy, "FORWARD_TIMEOUT_S", 0.1)
    _, c = _setup(fake_remote)
    r = _post(c)
    assert r.status_code == 504
    assert r.json() == {"error": "gateway_timeout"}


def test_upstream_500_passes_through(fake_remote):
    fake_remote.response_status = 500                # server fault, not stale credentials
    _, c = _setup(fake_remote)
    r = _post(c)
    assert r.status_code == 500
    assert r.json() == {"error": "upstream says no"}


def test_invalid_session_id_rejected_before_forwarding(fake_remote):
    _, c = _setup(fake_remote)
    r = _post(c, headers={"Mcp-Session-Id": "bad session!"})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_session_id"}
    assert fake_remote.calls == []


def test_usage_recorded_with_rohlik_adapter(fake_remote):
    conn, c = _setup(fake_remote)
    r = _post(c, body={"jsonrpc": "2.0", "method": "tools/call",
                       "params": {"name": "get_cart"}, "id": 1})
    assert r.status_code == 200
    rows = conn.execute(
        "SELECT adapter, account_key, tool, calls FROM tool_usage").fetchall()
    assert [tuple(row) for row in rows] == [("rohlik", "me@x.cz", "get_cart", 1)]


def test_garmin_token_rejected_on_rohlik_mcp(fake_remote):
    # mirror of test_proxy.py::test_bearer_for_other_adapter_is_rejected
    conn, c = _setup(fake_remote)
    garmin_token = "tok-garmin"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', "s" * 40)
    store.create_access_token(conn, store.hash_token(garmin_token), "garmin", "me@x.cz", "c1")
    r = c.post("/rohlik/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {garmin_token}"})
    assert r.status_code == 401                      # rohlik path must not accept a garmin token
    assert r.json() == {"error": "invalid_token"}
    assert fake_remote.calls == []                   # nothing reached the upstream
