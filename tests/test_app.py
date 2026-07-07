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
    assert "Your data," in r.text                 # hero H1
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
    assert "Your data," in r.text


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


def test_seo_crawler_surface(tmp_path):
    # generated from the adapter registry, so a new adapter shows up everywhere
    c = _client(tmp_path)
    robots = c.get("/robots.txt")
    assert robots.status_code == 200 and "text/plain" in robots.headers["content-type"]
    assert "Disallow: /garmin/oauth/" in robots.text
    assert "Sitemap: https://gw.example.com/sitemap.xml" in robots.text
    sitemap = c.get("/sitemap.xml").text
    assert "<loc>https://gw.example.com/</loc>" in sitemap
    assert "<loc>https://gw.example.com/garmin</loc>" in sitemap
    llms = c.get("/llms.txt").text
    assert "https://gw.example.com/garmin/mcp" in llms


def test_seo_head_meta(tmp_path):
    c = _client(tmp_path)
    home = c.get("/").text
    assert '<link rel="canonical" href="https://gw.example.com/">' in home
    assert "Garmin MCP Server" in home                     # title targets the query
    garmin = c.get("/garmin").text
    assert '<link rel="canonical" href="https://gw.example.com/garmin">' in garmin
    assert "<title>Garmin MCP Server — Connect Garmin to Claude | MissingMCP" in garmin
    assert 'property="og:title"' in garmin
    assert '"@type": "SoftwareApplication"' in garmin      # JSON-LD data block


def test_home_features_garmin_first(tmp_path):
    r = _client(tmp_path).get("/").text
    assert 'class="card featured"' in r
    assert "Your watch has the answers." in r              # featured tagline
    # (the "Soon" roadmap card for Whoop graduated to the live WHOOP card —
    # covered by test_home_shows_whoop_card)


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
    for path in ("/", "/garmin", "/privacy"):
        r = c.get(path).text
        assert 'class="logo" href="/"' in r, path
        assert 'src="/static/icon.png"' in r, path
        assert 'href="/#security"' in r, path         # shared nav
        assert "The connectors Claude is missing." in r, path   # shared footer
        # author credit is fixed (who built MissingMCP); the operator — who runs
        # this instance — stays config-driven and appears separately
        assert 'Built by <a href="https://slajs.eu">Vaclav Slajs</a>' in r, path
        assert "This instance is run by" in r, path


def _whoop_client():
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DB_PATH": ":memory:", "DATA_DIR": "/tmp",
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1"})
    return TestClient(build_app(cfg))


def test_whoop_page_lists_generated_tools():
    c = _whoop_client()
    r = c.get("/whoop")
    assert r.status_code == 200
    from missingmcp.adapters.whoop.mcp import TOOLS
    for name, _desc, _schema, _resolve in TOOLS:
        assert f"<code>{name}</code>" in r.text
    assert "gw.example.com/whoop/mcp" in r.text          # hero server URL filled


def test_home_shows_whoop_card(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert 'href="/whoop"' in r.text
    assert "WHOOP" in r.text


def test_privacy_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/privacy")
    assert r.status_code == 200
    assert "AES-256-GCM" in r.text
    assert "never stored" in r.text or "never store" in r.text
    assert "the operator" in r.text          # OPERATOR_NAME default filled by _render


def test_footer_links_privacy(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert 'href="/privacy"' in r.text


def test_static_site_js_served(tmp_path):
    c = _client(tmp_path)
    r = c.get("/static/site.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "data-copy" in r.text


def test_connector_pages_have_copy_buttons(tmp_path):
    r = _client(tmp_path).get("/garmin").text
    assert '/static/site.js' in r                       # layout loads the behavior
    assert r.count('data-copy="https://gw.example.com/garmin/mcp"') == 2  # hero + step 1
    w = _whoop_client().get("/whoop").text
    assert w.count('data-copy="https://gw.example.com/whoop/mcp"') == 2


def test_whoop_page_carries_brand_attribution_and_disclaimer():
    # WHOOP app-approval: data attributed to WHOOP + no implied affiliation.
    r = _whoop_client().get("/whoop").text
    assert "data by WHOOP" in r
    assert "registered trademark of WHOOP, Inc." in r
    assert "not affiliated with, endorsed by, or sponsored by WHOOP" in r


def test_privacy_mentions_auto_delete_on_revocation(tmp_path):
    r = _client(tmp_path).get("/privacy").text
    assert "automatically deletes your stored tokens" in r
