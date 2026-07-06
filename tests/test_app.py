from starlette.testclient import TestClient
from missingmcp.app import build_app
from missingmcp.config import load_config


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
    # Rohlík graduated: official MCP exists, so the card moved to the
    # "No longer missing" section and points at Rohlík directly, not /rohlik.
    assert 'href="/rohlik"' not in r.text
    assert "No longer missing" in r.text
    assert "https://mcp.rohlik.cz/mcp" in r.text
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


def test_retired_rohlik_paths_are_gone(tmp_path):
    # rohlik graduated to its official MCP; the gateway must not advertise it
    c = _client(tmp_path)
    assert c.get("/.well-known/oauth-authorization-server/rohlik").status_code == 404
    assert c.get("/rohlik").status_code == 404       # catch-all serves home with 404
    assert c.post("/rohlik/mcp", json={}).status_code == 405   # GET-only catch-all


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
    # connector-template sections: tips (skills/prompts land there later) and
    # the generated all-tools listing
    assert 'id="tips"' in r.text
    assert 'id="tools"' in r.text and "<details>" in r.text
    assert "get_sleep_data" in r.text                 # a stable generated tool entry
    # credit where due: built on the unmodified OS garmin_mcp, we only operate it
    assert "https://github.com/Taxuspt/garmin_mcp" in r.text
    assert "unmodified" in r.text


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


def test_operator_link_comes_from_config(tmp_path):
    # OPERATOR_URL set → the operator name is a link to it; unset → plain text.
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "t.db"),
                       "OPERATOR_NAME": "Jane Doe", "OPERATOR_URL": "https://jane.example"})
    r = TestClient(build_app(cfg)).get("/").text
    assert '<a href="https://jane.example">Jane Doe</a>' in r

    plain = _client(tmp_path).get("/").text        # no OPERATOR_URL configured
    assert "the operator" in plain                 # default name, unlinked
    assert "{OPERATOR}" not in plain


def test_subpages_share_site_chrome(tmp_path):
    # one _layout.html wraps every page: same header (logo linking home, nav)
    # and footer on the home page and the connector landing alike
    c = _client(tmp_path)
    for path in ("/", "/garmin"):
        r = c.get(path).text
        assert 'class="logo" href="/"' in r, path
        assert 'src="/static/icon.png"' in r, path
        assert 'href="/#security"' in r, path         # shared nav
        assert "The connectors Claude is missing." in r, path   # shared footer
        # author credit is fixed (who built MissingMCP); the operator — who runs
        # this instance — stays config-driven and appears separately
        assert 'Built by <a href="https://slajs.eu">Vaclav Slajs</a>' in r, path
        assert "This instance is run by" in r, path
