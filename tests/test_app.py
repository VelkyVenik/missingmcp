from starlette.testclient import TestClient
from garmin_gateway.app import build_app
from garmin_gateway.config import load_config


def _client(tmp_path):
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "t.db")})
    return TestClient(build_app(cfg))


def test_home_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "Your apps," in r.text                 # hero H1
    assert 'href="/garmin"' in r.text             # Garmin card links to the subpage
    assert 'href="/rohlik"' in r.text             # Rohlík card is live now
    assert "never stored" in r.text               # security section
    assert r.headers["x-frame-options"] == "DENY"


def test_unknown_path_serves_home_as_404(tmp_path):
    c = _client(tmp_path)
    r = c.get("/definitely-not-a-page")
    assert r.status_code == 404
    assert "Your apps," in r.text


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


def test_rohlik_metadata_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-authorization-server/rohlik").json()
    assert m["issuer"] == "https://gw.example.com/rohlik"
    assert m["authorization_endpoint"] == "https://gw.example.com/rohlik/oauth/authorize"
    assert m["token_endpoint"] == "https://gw.example.com/rohlik/oauth/token"


def test_rohlik_protected_resource_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-protected-resource/rohlik/mcp").json()
    assert m["resource"] == "https://gw.example.com/rohlik/mcp"
    assert m["authorization_servers"] == ["https://gw.example.com/rohlik"]


def test_mcp_requires_auth(tmp_path):
    c = _client(tmp_path)
    assert c.post("/garmin/mcp", json={}).status_code == 401
    # No alias — old path is gone. POST /mcp partial-matches the GET-only
    # catch-all route (path matches, method doesn't), so Starlette's router
    # returns 405 rather than 404; this is generic framework behavior for any
    # POST to any unregistered path, not specific to /mcp.
    assert c.post("/mcp", json={}).status_code == 405


def test_garmin_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/garmin")
    assert r.status_code == 200
    assert "How to connect" in r.text
    assert "https://gw.example.com/garmin/mcp" in r.text


def test_rohlik_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/rohlik")
    assert r.status_code == 200
    assert "How to connect" in r.text
    assert "https://gw.example.com/rohlik/mcp" in r.text
    # honest credential note: Rohlík stores BOTH email and password, encrypted
    assert "AES-256-GCM" in r.text and "never stored" not in r.text


def test_static_logo_assets_served(tmp_path):
    c = _client(tmp_path)
    for path in ("/static/icon.png", "/static/favicon-32.png",
                 "/static/apple-touch-icon.png"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"] == "image/png", path
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is a PNG"


def test_home_shows_logo_lockup(tmp_path):
    c = _client(tmp_path)
    r = c.get("/").text
    assert 'src="/static/icon.png"' in r          # mark in the header
    assert 'class="mcp"' in r and 'class="tld"' in r  # CSS wordmark parts
    assert '/static/favicon-32.png' in r          # PNG favicon link


def test_garmin_shows_logo_linking_home(tmp_path):
    c = _client(tmp_path)
    r = c.get("/garmin").text
    assert 'class="site-logo" href="/"' in r      # logo links back to the home
    assert 'src="/static/icon.png"' in r
