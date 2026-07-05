from starlette.testclient import TestClient
from garmin_gateway.app import build_app
from garmin_gateway.config import load_config


def _client(tmp_path):
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "t.db")})
    return TestClient(build_app(cfg))


def test_landing_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "/garmin/mcp" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_healthz(tmp_path):
    c = _client(tmp_path)
    assert c.get("/healthz").text == "ok"


def test_metadata_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-authorization-server/garmin").json()
    assert m["issuer"] == "https://gw.example.com/garmin"
    assert m["authorization_endpoint"] == "https://gw.example.com/garmin/oauth/authorize"


def test_protected_resource_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-protected-resource/garmin/mcp").json()
    assert m["resource"] == "https://gw.example.com/garmin/mcp"
    assert m["authorization_servers"] == ["https://gw.example.com/garmin"]


def test_mcp_requires_auth(tmp_path):
    c = _client(tmp_path)
    assert c.post("/garmin/mcp", json={}).status_code == 401
    # No alias — old path is gone. POST /mcp partial-matches the GET-only
    # catch-all route (path matches, method doesn't), so Starlette's router
    # returns 405 rather than 404; this is generic framework behavior for any
    # POST to any unregistered path, not specific to /mcp.
    assert c.post("/mcp", json={}).status_code == 405
