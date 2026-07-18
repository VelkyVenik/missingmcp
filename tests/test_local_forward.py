"""Local-forward (strategy C) behavior of proxy.handle_mcp, driven through
StubLocalAdapter — mirrors test_remote_forward.py. No subprocess, no upstream:
the forward handles the JSON-RPC request in-process."""
import json
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from conftest import StubLocalAdapter
from missingmcp import store, proxy, security
from missingmcp.config import load_config

TOKEN = "tok-local"
BLOB = '{"access_token":"at-0"}'


def _setup():
    conn = store.init_db(":memory:")
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x"})
    store.upsert_account(conn, "acmelocal", "me@x.cz", BLOB, cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(TOKEN), "acmelocal", "me@x.cz", "c1")
    adapter = StubLocalAdapter()
    rate = security.RateLimiter()

    def mcp(method):
        async def handler(request):
            return await proxy.handle_mcp(request, method, adapter, conn, None, cfg,
                                          cfg.gateway_secret, rate)
        return handler

    client = TestClient(Starlette(routes=[
        Route("/acmelocal/mcp", mcp("POST"), methods=["POST"]),
        Route("/acmelocal/mcp", mcp("GET"), methods=["GET"]),
        Route("/acmelocal/mcp", mcp("DELETE"), methods=["DELETE"]),
    ]))
    return conn, adapter, client


def _post(client, body=None):
    return client.post("/acmelocal/mcp",
                       json=body or {"jsonrpc": "2.0", "method": "initialize", "id": 1},
                       headers={"Authorization": f"Bearer {TOKEN}"})


def test_post_dispatches_in_process_with_decrypted_blob():
    _conn, adapter, c = _setup()
    r = _post(c)
    assert r.status_code == 200
    assert r.json()["result"] == {"echo": "initialize"}
    key, blob, body = adapter.forward.handled[0]
    assert key == "me@x.cz" and blob == BLOB
    assert json.loads(body)["method"] == "initialize"


def test_get_and_delete_are_405():
    _conn, adapter, c = _setup()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/acmelocal/mcp", headers=auth).status_code == 405
    assert c.delete("/acmelocal/mcp", headers=auth).status_code == 405
    assert adapter.forward.handled == []


def test_session_expired_maps_to_reauth_401():
    _conn, adapter, c = _setup()
    adapter.forward.expire = True
    r = _post(c)
    assert r.status_code == 401     # RFC 9728 challenge, not a dead-end 502
    assert r.json() == {
        "error": "invalid_token",
        "message": "Your AcmeLocal session expired. "
                   "Please reconnect the AcmeLocal MCP server.",
    }
    assert 'resource_metadata="https://x/.well-known/oauth-protected-resource/acmelocal/mcp"' \
        in r.headers["www-authenticate"]


def test_local_forward_exception_maps_to_502_shape(monkeypatch):
    _conn, adapter, c = _setup()

    async def _boom(conn, account_key, blob, body):
        raise RuntimeError("boom")

    monkeypatch.setattr(adapter.forward, "handle", _boom)
    r = _post(c)
    assert r.status_code == 502
    assert r.json() == {"error": "bad_gateway"}


def test_usage_and_response_event_recorded(capsys):
    conn, _adapter, c = _setup()
    r = _post(c, body={"jsonrpc": "2.0", "method": "tools/call",
                       "params": {"name": "get_profile"}, "id": 1})
    assert r.status_code == 200
    rows = [tuple(row) for row in conn.execute(
        "SELECT adapter, account_key, tool, calls FROM tool_usage").fetchall()]
    assert rows == [("acmelocal", "me@x.cz", "get_profile", 1)]
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    resp = next(e for e in events if e["event"] == "mcp-response")
    assert resp["adapter"] == "acmelocal" and resp["tool"] == "get_profile"
    assert resp["status"] == 200 and resp["bytes"] > 0


def test_unauthenticated_is_401():
    _conn, adapter, c = _setup()
    r = c.post("/acmelocal/mcp", json={"jsonrpc": "2.0", "method": "initialize"})
    assert r.status_code == 401
    assert adapter.forward.handled == []
