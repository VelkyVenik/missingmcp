from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, proxy, workers, security
from garmin_gateway.config import load_config


def _cfg(tmp_path, fw):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
                        "DATA_DIR": str(tmp_path),
                        "WORKER_PORT_START": str(fw.port), "WORKER_PORT_END": str(fw.port)})


def _app(conn, mgr, cfg):
    rate = security.RateLimiter()
    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", conn, mgr, cfg, cfg.gateway_secret, rate)
    return TestClient(Starlette(routes=[Route("/mcp", mcp_post, methods=["POST"])]))


class FakeProc:
    def poll(self): return None
    def terminate(self): pass


def test_unauthorized_without_bearer(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0"})
    assert r.status_code == 401


def test_authorized_forwards_to_worker(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-123"
    store.upsert_account(conn, "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id") == "sess-1"
    assert fake_worker.calls and fake_worker.calls[-1][1] == "/mcp"
