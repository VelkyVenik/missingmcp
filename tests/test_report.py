"""Daily user-stats report — DB metrics and timezone boundaries. Everything is
deterministic: `now` is injected, so no wall-clock reads."""
from datetime import datetime
from zoneinfo import ZoneInfo

from missingmcp import report, store

PRAGUE = ZoneInfo("Europe/Prague")
# 2026-07-18 09:00 Prague (CEST, UTC+2) → "yesterday" = the 2026-07-17 Prague day
NOW = datetime(2026, 7, 18, 9, 0, tzinfo=PRAGUE)


def _seed(conn):
    accts = [
        ("garmin", "a@x", "2026-07-17 10:00:00"),  # yesterday
        ("garmin", "b@x", "2026-07-17 23:00:00"),  # 01:00 CEST on the 18th → today, not yesterday
        ("garmin", "d@x", "2026-07-10 09:00:00"),  # old → total only, outside the 7-day window
        ("whoop",  "c@x", "2026-07-15 12:00:00"),  # inside 7-day window, not yesterday
    ]
    for ad, key, created in accts:
        conn.execute(
            "INSERT INTO accounts (adapter, account_key, blob_enc, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (ad, key, "x", created, created))
    usage = [
        ("garmin", "a@x", "2026-07-17 12:00:00"),  # active yesterday
        ("garmin", "d@x", "2026-07-17 15:00:00"),  # active yesterday (old account)
        ("whoop",  "c@x", "2026-07-14 08:00:00"),  # not yesterday
    ]
    for ad, key, last in usage:
        conn.execute(
            "INSERT INTO tool_usage (adapter, account_key, tool, calls, last_used) "
            "VALUES (?,?,?,?,?)", (ad, key, "get_x", 3, last))
    conn.commit()


def test_build_report_counts_and_tz_boundary():
    conn = store.init_db(":memory:")
    _seed(conn)
    r = report.build_report(conn, NOW)
    assert r["date"] == "2026-07-17"
    assert r["adapters"]["garmin"] == {"new": 1, "active": 2, "total": 3, "new_week": 1}
    assert r["adapters"]["whoop"] == {"new": 0, "active": 0, "total": 1, "new_week": 1}
    assert r["totals"] == {"new": 1, "active": 2, "total": 4, "new_week": 2}


def test_active_ignores_clients_that_only_handshook():
    """Regression: the report counted protocol traffic as activity, so a merely
    connected client inflated "active" (2026-07-25: 83 reported vs 48 real)."""
    conn = store.init_db(":memory:")
    _seed(conn)
    for tool in ("initialize", "tools/list", "notifications/initialized"):
        conn.execute(
            "INSERT INTO tool_usage (adapter, account_key, tool, calls, last_used) "
            "VALUES (?,?,?,?,?)", ("garmin", "lurker@x", tool, 1, "2026-07-17 12:00:00"))
    conn.commit()
    r = report.build_report(conn, NOW)
    assert r["adapters"]["garmin"]["active"] == 2   # unchanged by the lurker
    assert r["totals"]["active"] == 2


def test_render_text_has_date_and_totals():
    conn = store.init_db(":memory:")
    _seed(conn)
    text = report.render_text(report.build_report(conn, NOW))
    assert "daily report for 2026-07-17" in text
    assert "Garmin: +1 new · 2 active · 3 total" in text
    assert "Total: +1 new · 2 active · 4 users" in text


def test_render_text_empty_db():
    conn = store.init_db(":memory:")
    text = report.render_text(report.build_report(conn, NOW))
    assert "Total: +0 new · 0 active · 0 users" in text  # no crash on empty


def test_open_ro_handles_special_chars_in_path(tmp_path):
    # A '#' (legal on POSIX) would break a raw file:{path}?mode=ro URI; as_uri()
    # escapes it so the right file opens read-only.
    db = tmp_path / "we#ird gateway.db"
    store.init_db(str(db)).close()
    conn = report.open_ro(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        conn.close()
