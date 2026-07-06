"""The in-process WHOOP MCP server: hand-rolled stateless JSON-RPC over HTTP
(initialize / notifications / tools/list / tools/call / ping) against the fake
WHOOP upstream."""
import asyncio
import json
import pytest
from missingmcp import store
from missingmcp.adapters.base import SessionExpired
from missingmcp.adapters.whoop.mcp import TOOLS, WhoopLocalForward
from missingmcp.config import load_config

KEY = "user@example.com"
BLOB = json.dumps({"access_token": "at-0", "refresh_token": "rt-0",
                   "expires_at": 9999999999, "user_id": 123, "email": KEY})


def _setup(fake):
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                       "WHOOP_API_BASE": f"http://127.0.0.1:{fake.port}"})
    conn = store.init_db(":memory:")
    store.upsert_account(conn, "whoop", KEY, BLOB, cfg.gateway_secret)
    fake.valid_tokens.add("at-0")
    return conn, WhoopLocalForward(cfg)


def _rpc(fwd, conn, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    status, headers, payload = asyncio.run(
        fwd.handle(conn, KEY, BLOB, json.dumps(body).encode()))
    return status, headers, json.loads(payload) if payload else None


def test_initialize_negotiates_known_version(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    status, headers, body = _rpc(fwd, conn, "initialize",
                                 {"protocolVersion": "2025-03-26"})
    assert status == 200 and headers["Content-Type"] == "application/json"
    result = body["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "missingmcp-whoop"


def test_initialize_unknown_version_falls_back_to_latest(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "initialize", {"protocolVersion": "1999-01-01"})
    assert body["result"]["protocolVersion"] == "2025-06-18"


def test_notification_gets_202_empty(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    status, _h, body = _rpc(fwd, conn, "notifications/initialized", rid=None)
    assert status == 202 and body is None


def test_ping_and_unknown_method(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    assert _rpc(fwd, conn, "ping")[2]["result"] == {}
    _s, _h, body = _rpc(fwd, conn, "bogus/method")
    assert body["error"]["code"] == -32601


def test_tools_list_exposes_all_eight(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/list")
    tools = body["result"]["tools"]
    assert [t["name"] for t in tools] == [name for name, _d, _s2, _r in TOOLS]
    assert len(tools) == 8
    assert all(t["description"] and t["inputSchema"]["type"] == "object" for t in tools)


def test_tools_call_get_profile(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/call",
                        {"name": "get_profile", "arguments": {}})
    result = body["result"]
    assert result["isError"] is False
    assert "User@Example.com" in result["content"][0]["text"]


def test_collection_args_map_to_whoop_query(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _rpc(fwd, conn, "tools/call", {"name": "get_cycles", "arguments": {
        "start": "2026-07-01T00:00:00.000Z", "end": "2026-07-06T00:00:00.000Z",
        "limit": 25, "next_token": "abc"}})
    path = next(p for m, p, _h, _b in fake_whoop.calls if "/v2/cycle" in p)
    assert "start=2026-07-01" in path and "end=2026-07-06" in path
    assert "limit=25" in path and "nextToken=abc" in path      # camelCase upstream


def test_by_id_tool_builds_path_and_requires_id(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/call",
                        {"name": "get_sleep", "arguments": {"id": "uuid-1"}})
    assert body["result"]["isError"] is False
    assert "/v2/activity/sleep/uuid-1" in body["result"]["content"][0]["text"]
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "get_sleep", "arguments": {}})
    assert body["result"]["isError"] is True                   # missing id → tool error


def test_unknown_tool_is_invalid_params(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "nope", "arguments": {}})
    assert body["error"]["code"] == -32602


def test_upstream_429_and_500_become_tool_errors(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    fake_whoop.data_status = 429
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "get_cycles", "arguments": {}})
    assert body["result"]["isError"] is True
    assert "rate limit" in body["result"]["content"][0]["text"]
    fake_whoop.data_status = 500
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "get_cycles", "arguments": {}})
    assert body["result"]["isError"] is True


def test_dead_refresh_raises_session_expired(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                       "WHOOP_API_BASE": f"http://127.0.0.1:{fake_whoop.port}"})
    fake_whoop.refresh_fails = True
    stale = json.dumps({**json.loads(BLOB), "expires_at": 1})
    store.upsert_account(conn, "whoop", KEY, stale, cfg.gateway_secret)
    with pytest.raises(SessionExpired):
        asyncio.run(fwd.handle(conn, KEY, stale, json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_cycles", "arguments": {}}}).encode()))


def test_batch_and_garbage_are_400(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    status, _h, _b = asyncio.run(fwd.handle(conn, KEY, BLOB, b"[]"))
    assert status == 400
    status, _h, _b = asyncio.run(fwd.handle(conn, KEY, BLOB, b"not json"))
    assert status == 400
