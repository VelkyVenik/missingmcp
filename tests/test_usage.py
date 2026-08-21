"""Usage meter: the public "N people used this in the last 30 days" line on
the landing pages — threshold, window, protocol-traffic exclusion, caching,
and the per-request placeholder fill in the app."""
from starlette.testclient import TestClient

from missingmcp import store, usage
from missingmcp.app import build_app
from missingmcp.config import load_config

SECRET = "s" * 40


def _seed_active(conn, adapter, n, tool="get_stats"):
    for i in range(n):
        store.record_usage(conn, adapter, f"user{i}@x.cz", tool)


def test_count_excludes_protocol_traffic_and_old_activity():
    conn = store.init_db(":memory:")
    _seed_active(conn, "garmin", 3)
    store.record_usage(conn, "garmin", "handshake@x.cz", "tools/list")  # protocol only
    store.record_usage(conn, "garmin", "old@x.cz", "get_stats")
    conn.execute("UPDATE tool_usage SET last_used=datetime('now','-31 days') "
                 "WHERE account_key='old@x.cz'")
    conn.commit()
    m = usage.UsageMeter(conn)
    assert m.count("garmin") == 3
    assert m.count("whoop") == 0
    conn.close()


def test_snippet_empty_below_threshold_plural_above():
    conn = store.init_db(":memory:")
    _seed_active(conn, "garmin", usage.MIN_COUNT - 1)
    assert usage.UsageMeter(conn).snippet("garmin") == ""
    _seed_active(conn, "garmin", usage.MIN_COUNT)     # one more distinct account
    snippet = usage.UsageMeter(conn).snippet("garmin")
    assert f"{usage.MIN_COUNT} people used this in the last 30 days" in snippet
    assert 'class="usage-meter"' in snippet
    conn.close()


def test_count_is_cached_until_ttl_expires():
    conn = store.init_db(":memory:")
    _seed_active(conn, "garmin", 2)
    m = usage.UsageMeter(conn)
    assert m.count("garmin") == 2
    _seed_active(conn, "garmin", 5)          # 3 new accounts land in the DB
    assert m.count("garmin") == 2            # cache still serving the old count
    m._expires = 0.0                         # force the TTL over
    assert m.count("garmin") == 5
    conn.close()


def _client(tmp_path, active_garmin):
    db = tmp_path / "t.db"
    conn = store.init_db(str(db))
    _seed_active(conn, "garmin", active_garmin)
    conn.close()
    cfg = load_config({"GATEWAY_SECRET": SECRET, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": str(db),
                       "WHOOP_CLIENT_ID": "cid", "WHOOP_CLIENT_SECRET": "cs"})
    return TestClient(build_app(cfg))


def test_pages_show_meter_and_never_leak_placeholders(tmp_path):
    c = _client(tmp_path, active_garmin=usage.MIN_COUNT)
    for path in ("/", "/garmin"):
        r = c.get(path)
        assert f"{usage.MIN_COUNT} people used this in the last 30 days" in r.text
        assert "{USAGE_METER" not in r.text
    # whoop is below threshold: placeholder cleared, nothing rendered
    r = c.get("/whoop")
    assert 'class="usage-meter"' not in r.text
    assert "{USAGE_METER" not in r.text
    # the 404 catch-all serves home — placeholders must be filled there too
    r = c.get("/definitely-not-a-page")
    assert r.status_code == 404
    assert "{USAGE_METER" not in r.text


def test_pages_render_plain_when_below_threshold(tmp_path):
    c = _client(tmp_path, active_garmin=usage.MIN_COUNT - 1)
    for path in ("/", "/garmin", "/whoop"):
        r = c.get(path)
        assert 'class="usage-meter"' not in r.text
        assert "{USAGE_METER" not in r.text
