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
    assert "/mcp" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_healthz(tmp_path):
    c = _client(tmp_path)
    assert c.get("/healthz").text == "ok"


def test_metadata_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-authorization-server").json()
    assert m["issuer"] == "https://gw.example.com"


def test_mcp_requires_auth(tmp_path):
    c = _client(tmp_path)
    assert c.post("/mcp", json={}).status_code == 401
