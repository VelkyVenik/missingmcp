"""Remote-forward (strategy A) behavior of proxy.handle_mcp, driven through the
StubRemoteAdapter against the fake_remote upstream — mirrors how test_proxy.py
exercises the garmin worker path. There is no in-tree remote adapter today
(rohlik graduated to its official MCP); this suite keeps the strategy honest."""
import json
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from conftest import StubRemoteAdapter
from missingmcp import store, proxy, security
from missingmcp.config import load_config

TOKEN = "tok-acme"
BLOB = json.dumps({"user": "me@x.cz", "pass": "pw"})


def _setup(upstream):
    """(conn, TestClient) with an acme account + Bearer token already stored.
    manager=None: the remote path must never need a WorkerManager."""
    conn = store.init_db(":memory:")
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x"})
    store.upsert_account(conn, "acme", "me@x.cz", BLOB, cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(TOKEN), "acme", "me@x.cz", "c1")
    adapter = StubRemoteAdapter(f"http://127.0.0.1:{upstream.port}/mcp")
    rate = security.RateLimiter()

    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", adapter, conn, None, cfg,
                                      cfg.gateway_secret, rate)
    client = TestClient(Starlette(routes=[Route("/acme/mcp", mcp_post, methods=["POST"])]))
    return conn, client


def _post(client, headers=None, body=None):
    return client.post("/acme/mcp",
                       json=body or {"jsonrpc": "2.0", "method": "initialize", "id": 1},
                       headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})})


def test_upstream_receives_injected_and_threaded_headers(fake_remote):
    _, c = _setup(fake_remote)
    r = _post(c, headers={"Accept": "application/json, text/event-stream",
                          "Mcp-Session-Id": "sess-abc"})
    assert r.status_code == 200
    method, path, hdrs, body = fake_remote.calls[-1]
    assert (method, path) == ("POST", "/mcp")
    assert hdrs.get("x-acme-user") == "me@x.cz"      # injected from the decrypted blob
    assert hdrs.get("x-acme-pass") == "pw"
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
def test_upstream_auth_rejection_maps_to_reauth_401(fake_remote, status):
    fake_remote.response_status = status
    _, c = _setup(fake_remote)
    r = _post(c)
    assert r.status_code == 401                      # not streamed through; re-auth challenge
    assert r.json() == {                             # stable shape, byte-for-byte
        "error": "invalid_token",
        "message": "Your Acme session expired. Please reconnect the Acme MCP server.",
    }
    assert 'resource_metadata="https://x/.well-known/oauth-protected-resource/acme/mcp"' \
        in r.headers["www-authenticate"]


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


def test_usage_recorded_with_remote_adapter(fake_remote):
    conn, c = _setup(fake_remote)
    r = _post(c, body={"jsonrpc": "2.0", "method": "tools/call",
                       "params": {"name": "get_cart"}, "id": 1})
    assert r.status_code == 200
    rows = conn.execute(
        "SELECT adapter, account_key, tool, calls FROM tool_usage").fetchall()
    assert [tuple(row) for row in rows] == [("acme", "me@x.cz", "get_cart", 1)]


def test_foreign_token_rejected_on_remote_adapter_mcp(fake_remote):
    # mirror of test_proxy.py::test_bearer_for_other_adapter_is_rejected
    conn, c = _setup(fake_remote)
    garmin_token = "tok-garmin"
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', "s" * 40)
    store.create_access_token(conn, store.hash_token(garmin_token), "garmin", "me@x.cz", "c1")
    r = c.post("/acme/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {garmin_token}"})
    assert r.status_code == 401                      # remote path must not accept a foreign token
    assert r.json() == {"error": "invalid_token"}
    assert fake_remote.calls == []                   # nothing reached the upstream


def test_mcp_response_event_records_latency(fake_remote, capsys):
    _, c = _setup(fake_remote)
    r = _post(c, body={"jsonrpc": "2.0", "method": "tools/call",
                       "params": {"name": "get_cart"}, "id": 1})
    assert r.status_code == 200
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    resp = next(e for e in events if e["event"] == "mcp-response")
    assert resp["adapter"] == "acme" and resp["account"] == "me@x.cz"
    assert resp["tool"] == "get_cart" and resp["status"] == 200
    assert isinstance(resp["ttfb_ms"], int) and isinstance(resp["total_ms"], int)
    assert resp["total_ms"] >= resp["ttfb_ms"] >= 0
    assert resp["bytes"] > 0
