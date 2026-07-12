import json
from starlette.testclient import TestClient
from missingmcp import store
from missingmcp.app import build_app, _run_data_cleanup
from missingmcp.config import load_config

SECRET = "k" * 40


def _events(captured) -> list[str]:
    return [json.loads(line)["event"] for line in captured.out.splitlines()
            if line.strip().startswith("{")]


def test_run_data_cleanup_purges_retired_and_sweeps_orphans_with_logs(capsys):
    conn = store.init_db(":memory:")
    # A retired adapter (rohlik) with a stored account + client.
    store.upsert_account(conn, "rohlik", "me@x.cz", "{}", SECRET)
    store.create_client(conn, "rc", "sh", ["https://a/cb"], "Claude", "rohlik")
    # An old, token-less garmin client — an abandoned DCR.
    store.create_client(conn, "old_orphan", "sh", ["https://a/cb"], "Claude", "garmin")
    conn.execute("UPDATE oauth_clients SET created_at=datetime('now','-2 hours')")
    conn.commit()

    _run_data_cleanup(conn, orphan_ttl=3600, retired_adapters={"rohlik"})

    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE adapter='rohlik'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0
    events = _events(capsys.readouterr())
    assert "cleanup-dead-adapter" in events
    assert "cleanup-orphan-clients" in events
    conn.close()


def test_run_data_cleanup_is_silent_when_nothing_to_delete(capsys):
    conn = store.init_db(":memory:")
    # A fresh orphan (younger than the TTL) and no retired-adapter data.
    store.create_client(conn, "fresh", "sh", ["https://a/cb"], "Claude", "garmin")
    conn.commit()

    _run_data_cleanup(conn, orphan_ttl=3600, retired_adapters={"rohlik"})

    events = _events(capsys.readouterr())
    assert "cleanup-orphan-clients" not in events
    assert "cleanup-dead-adapter" not in events
    assert conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 1  # fresh kept
    conn.close()


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


def test_authorize_get_is_rate_limited(tmp_path):
    # authz_get mutates process-local state (csrf.issue / put_mfa) on every call,
    # so it must throttle per IP like its OAuth siblings (oauth: bucket, 20/60).
    c = _client(tmp_path)
    codes = [c.get("/garmin/oauth/authorize").status_code for _ in range(25)]
    assert 429 in codes                                        # 20/60s window exceeded


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


def test_wellknown_glama_json(tmp_path):
    # Glama connector-directory ownership proof (glama.ai/mcp/connectors)
    r = _client(tmp_path).get("/.well-known/glama.json")
    assert r.status_code == 200 and "application/json" in r.headers["content-type"]
    assert r.json() == {"$schema": "https://glama.ai/mcp/schemas/server.json",
                        "maintainers": ["vaclav@slajs.eu"]}


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
        assert "Your data, in Claude." in r, path               # shared footer (umbrella promise)
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


def test_whoop_is_beta_with_limit_note():
    g = _whoop_client().get("/whoop").text   # /whoop only exists when WHOOP creds are set
    assert "pill beta" in g            # hero pill is Beta, not Live
    assert "pill live" not in g        # no longer advertised as Live
    assert "approval" in g.lower()     # explains it's pending WHOOP approval
    assert "10 users" in g             # the current user cap


def test_home_lists_upcoming_connectors(tmp_path):
    r = _client(tmp_path).get("/").text
    assert "<h3>Oura</h3>" in r                     # card heading, not the suggest-modal placeholder
    assert "<h3>Apple Health</h3>" in r
    assert "Beta" in r                              # WHOOP card downgraded from Live
    # the coming-soon cards reuse the existing subscribe modal (hero + "Missing
    # something?" card were the only two before; +2 new = at least 4)
    assert r.count('data-modal="subscribe"') >= 4


def test_hero_leads_with_outcome(tmp_path):
    home = _client(tmp_path).get("/").text
    # no badge — the H1 leads (avoid over-niching the umbrella)
    assert 'class="badge"' not in home
    # subhead leads with the outcome, not "connectors" / "MCP server"
    assert "except the numbers your apps keep locked away" in home
    assert "an answer that actually knows" in home
    # <title> leads with the promise AND keeps the SEO keyword tail
    assert "Your data, in Claude" in home          # from <title>/og:title
    assert "Garmin MCP Server" in home             # SEO keyword retained
    # the old jargon-first subhead phrasing is gone
    assert "hosts the connectors your favorite services are missing" not in home


def test_just_ask_section(tmp_path):
    home = _client(tmp_path).get("/").text
    assert "Just ask" in home                                   # section heading
    assert "Am I on track for today" in home                    # the "You" bubble
    assert "running a deficit for the load ahead" in home        # the "Claude" bubble
    assert "No single app does that." in home                    # the caption
    assert "How did I sleep this week?" in home                  # first (easy) chip
    assert "Compare my last three long runs." in home
    assert "Why was my recovery low today?" in home
    # the demo sits above the connector list
    assert home.index('id="just-ask"') < home.index('id="connectors"')


def test_garmin_page_leads_with_outcome(tmp_path):
    g = _client(tmp_path).get("/garmin").text
    # page-hero subhead now opens on the outcome, not "A hosted Garmin MCP server:"
    assert "everything your watch knows" in g.lower()
    # the old jargon-first opener is gone
    assert "A hosted <strong>Garmin MCP server</strong>: everything" not in g
    # MCP is kept, but demoted to an under-the-hood aside
    assert "under the hood" in g.lower()
    assert "hosted MCP server" in g


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


import sqlite3


def _client_and_db(tmp_path):
    db = str(tmp_path / "t.db")
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": db})
    return TestClient(build_app(cfg)), db


def _rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_subscribe_stores_email(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "Fan@Example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    # normalized lowercase, stored
    assert _rows(db, "SELECT email FROM subscribers") == [("fan@example.com",)]


def test_subscribe_duplicate_is_silent_ok(tmp_path):
    c, db = _client_and_db(tmp_path)
    c.post("/subscribe", data={"email": "fan@example.com"})
    r = c.post("/subscribe", data={"email": "fan@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(_rows(db, "SELECT email FROM subscribers")) == 1


def test_subscribe_rejects_bad_email(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "not-an-email"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_email"
    assert _rows(db, "SELECT email FROM subscribers") == []


def test_subscribe_honeypot_is_silent_noop(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "bot@example.com", "website": "http://spam"})
    assert r.status_code == 200 and r.json() == {"ok": True}   # looks fine to the bot
    assert _rows(db, "SELECT email FROM subscribers") == []    # but nothing stored


def test_subscribe_rate_limited(tmp_path):
    c, _ = _client_and_db(tmp_path)
    codes = [c.post("/subscribe", data={"email": f"u{i}@example.com"}).status_code
             for i in range(7)]
    assert 429 in codes                                        # 5/60s window exceeded


def test_suggest_stores_suggestion_without_subscribing(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/suggest", data={"email": "a@example.com", "description": "Strava"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, "SELECT email, description, wants_updates FROM suggestions") == \
        [("a@example.com", "Strava", 0)]
    assert _rows(db, "SELECT email FROM subscribers") == []


def test_suggest_with_updates_also_subscribes(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/suggest", data={"email": "b@example.com", "description": "Oura",
                                 "wants_updates": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, "SELECT email FROM subscribers") == [("b@example.com",)]


def test_suggest_honeypot_is_silent_noop(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/suggest", data={"email": "bot@example.com", "description": "x",
                                 "website": "spam"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, "SELECT email FROM suggestions") == []


def test_subscribe_rejects_oversized_body(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "a@b.co", "description": "x" * 70_000})
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert _rows(db, "SELECT email FROM subscribers") == []


def test_home_has_signup_modals_not_github_link(tmp_path):
    r = _client(tmp_path).get("/").text
    # the old GitHub-issues link in the card is gone (GitHub stays only in footer/security)
    assert "github.com/VelkyVenik/missingmcp/issues/new" not in r
    # two buttons open the two modals
    assert 'data-modal="suggest"' in r
    assert 'data-modal="subscribe"' in r
    # the two dialogs exist and post to the right endpoints
    assert 'id="modal-subscribe"' in r and 'data-endpoint="/subscribe"' in r
    assert 'id="modal-suggest"' in r and 'data-endpoint="/suggest"' in r
    # honeypot present in each form, and the opt-in checkbox on the suggest form
    assert r.count('name="website"') == 2
    assert 'name="wants_updates"' in r


def test_github_still_reachable_in_footer(tmp_path):
    # removing the card link must not remove GitHub from the site entirely
    r = _client(tmp_path).get("/").text
    assert 'href="https://github.com/VelkyVenik/missingmcp"' in r


def test_site_js_has_modal_behavior(tmp_path):
    r = _client(tmp_path).get("/static/site.js").text
    assert "data-modal" in r and "showModal" in r
    assert "data-endpoint" in r and "fetch(" in r


def test_site_js_is_cache_busted(tmp_path):
    from missingmcp.pages import _SITE_JS_VER
    r = _client(tmp_path).get("/").text
    assert f'/static/site.js?v={_SITE_JS_VER}"' in r    # versioned script src
    assert "{SITE_JS_VER}" not in r                     # placeholder fully replaced
    assert len(_SITE_JS_VER) == 8


def test_suggest_description_is_required(tmp_path):
    # required so email+Enter can't submit an empty wish (native validation blocks it)
    r = _client(tmp_path).get("/").text
    assert '<textarea name="description" rows="3" required' in r


def test_privacy_mentions_signup_storage_and_deletion(tmp_path):
    r = _client(tmp_path).get("/privacy").text
    assert "newsletter" in r.lower() or "notify" in r.lower()   # opt-in disclosed
    assert "email the operator" in r.lower()                    # deletion path
