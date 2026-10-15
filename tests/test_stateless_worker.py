"""Regression: a worker replaced between two calls (cap eviction, idle reap,
crash) must not fail the client's next call with 404 "Session not found".

Real proxy + WorkerManager default spawn + GarminWorkerForward env, against
tests/garmin_stub_worker.py on the real MCP SDK. The stateful run is the
control: it reproduces the production failure, so the test can't pass by
never replacing the worker.
"""
import json
import sys

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from conftest import _free_port
from missingmcp import proxy, security, store, workers
from missingmcp.adapters.garmin import GarminAdapter
from missingmcp.config import load_config

STUB = __file__.replace("test_stateless_worker.py", "garmin_stub_worker.py")
# What a client still holds from a worker that no longer exists.
LEGACY_SID = "0123456789abcdef0123456789abcdef"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "regression", "version": "0"}}}
WHOAMI = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "whoami", "arguments": {}}}


class Gateway:
    def __init__(self, tmp_path, stateless: bool, max_workers: int):
        start = _free_port()
        cfg = load_config({
            "GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
            "DATA_DIR": str(tmp_path), "GARMIN_MCP_CMD": f"{sys.executable} {STUB}",
            "WORKER_PORT_START": str(start), "WORKER_PORT_END": str(start + 4),
            "MAX_WORKERS": str(max_workers),
            "GARMIN_MCP_STATELESS_HTTP": "true" if stateless else "false"})
        self.conn = store.init_db(":memory:")
        adapter = GarminAdapter(cfg)
        self.mgr = workers.WorkerManager(cfg, adapter.forward)
        rate = security.RateLimiter()
        for owner in ("alice", "bob"):
            key = f"{owner}@example.test"   # synthetic accounts only
            store.upsert_account(self.conn, "garmin", key, json.dumps({"owner": owner}),
                                 cfg.gateway_secret)
            store.create_access_token(self.conn, store.hash_token(f"tok-{owner}"),
                                      "garmin", key, "c1")

        def route(method):
            async def h(request):
                return await proxy.handle_mcp(request, method, adapter, self.conn,
                                              self.mgr, cfg, cfg.gateway_secret, rate)
            return Route("/garmin/mcp", h, methods=[method])
        self.client = TestClient(Starlette(routes=[route(m) for m in ("POST", "GET", "DELETE")]))

    def post(self, owner, payload, sid=None):
        headers = {"Authorization": f"Bearer tok-{owner}",
                   "Accept": "application/json, text/event-stream"}
        if sid:
            headers["Mcp-Session-Id"] = sid
        return self.client.post("/garmin/mcp", json=payload, headers=headers)

    def connect(self, owner) -> "str | None":
        """initialize + initialized, as a client does; returns the session id."""
        r = self.post(owner, INIT)
        assert r.status_code == 200, r.text
        sid = r.headers.get("mcp-session-id")
        self.post(owner, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        return sid

    def pid(self, owner) -> int:
        return self.mgr._workers[f"{owner}@example.test"].process.pid

    def close(self):
        self.mgr.shutdown()


def _whoami(r) -> str:
    data = next(line[6:] for line in r.text.splitlines() if line.startswith("data: "))
    return json.loads(data)["result"]["content"][0]["text"]


@pytest.fixture
def gateway(tmp_path, request):
    stateless, max_workers = request.param
    gw = Gateway(tmp_path, stateless, max_workers)
    yield gw
    gw.close()


@pytest.mark.parametrize("gateway", [(True, 1), (False, 1)], indirect=True,
                         ids=["stateless", "stateful-control"])
def test_next_call_survives_worker_eviction(gateway, request):
    stateless = "stateless" == request.node.callspec.id
    sid = gateway.connect("alice")
    if stateless:
        assert sid is None             # stateless workers never issue one…
        sid = LEGACY_SID               # …but clients still send a pre-deploy id
    r = gateway.post("alice", WHOAMI, sid)
    assert r.status_code == 200 and _whoami(r) == "alice"
    first = gateway.pid("alice")

    # MAX_WORKERS=1: bob's spawn evicts alice's worker (the prod LRU-cap path).
    gateway.connect("bob")
    assert "alice@example.test" not in gateway.mgr._workers

    r = gateway.post("alice", WHOAMI, sid)   # same client, same session id
    assert gateway.pid("alice") != first     # served by a fresh worker
    if stateless:
        assert r.status_code == 200, r.text
        assert "Session not found" not in r.text
        assert _whoami(r) == "alice"
        assert "mcp-session-id" not in r.headers
    else:
        assert r.status_code == 404 and "Session not found" in r.text


@pytest.mark.parametrize("gateway", [(True, 1)], indirect=True)
def test_call_survives_worker_crash_without_initialize(gateway):
    # A crashed worker is respawned on the next request; a stateless worker
    # serves tools/call with no initialize on the new process at all.
    gateway.connect("alice")
    gateway.mgr._workers["alice@example.test"].process.kill()
    r = gateway.post("alice", WHOAMI, LEGACY_SID)
    assert r.status_code == 200 and _whoami(r) == "alice"


@pytest.mark.parametrize("gateway", [(True, 2)], indirect=True)
def test_accounts_stay_isolated_and_auth_is_enforced(gateway):
    # The Bearer token alone picks the worker; a session id carries no identity.
    assert _whoami(gateway.post("alice", WHOAMI, LEGACY_SID)) == "alice"
    assert _whoami(gateway.post("bob", WHOAMI, LEGACY_SID)) == "bob"
    assert _whoami(gateway.post("alice", WHOAMI)) == "alice"
    assert gateway.pid("alice") != gateway.pid("bob")
    r = gateway.client.post("/garmin/mcp", json=WHOAMI,
                            headers={"Mcp-Session-Id": LEGACY_SID})
    assert r.status_code == 401
    r = gateway.client.post("/garmin/mcp", json=WHOAMI,
                            headers={"Authorization": "Bearer tok-mallory"})
    assert r.status_code == 401


@pytest.mark.parametrize("gateway", [(True, 1)], indirect=True)
def test_get_and_delete_are_405_without_touching_a_worker(gateway):
    auth = {"Authorization": "Bearer tok-alice", "Mcp-Session-Id": LEGACY_SID}
    r = gateway.client.get("/garmin/mcp", headers={**auth, "Accept": "text/event-stream"})
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert gateway.client.delete("/garmin/mcp", headers=auth).status_code == 405
    assert gateway.mgr._workers == {}            # nothing spawned for either
    assert gateway.client.get("/garmin/mcp").status_code == 401   # auth still first
