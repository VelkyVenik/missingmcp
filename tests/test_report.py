"""Daily user-stats report — DB metrics on UTC days. Everything is deterministic:
`now` is injected and activity rows are written with explicit days, so no
wall-clock reads."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from missingmcp import report, store

# 2026-07-18 09:00 UTC → "yesterday" = the 2026-07-17 UTC day
NOW = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)


def _seed(conn):
    accts = [
        ("garmin", "a@x", "2026-07-17 10:00:00"),  # yesterday
        ("garmin", "b@x", "2026-07-18 00:30:00"),  # today (UTC), not yesterday
        ("garmin", "d@x", "2026-07-10 09:00:00"),  # old → total only, outside the 7-day window
        ("whoop",  "c@x", "2026-07-15 12:00:00"),  # inside 7-day window, not yesterday
    ]
    for ad, key, created in accts:
        conn.execute(
            "INSERT INTO accounts (adapter, account_key, blob_enc, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (ad, key, "x", created, created))
    activity = [
        ("garmin", "a@x", "2026-07-17"),   # active yesterday
        ("garmin", "d@x", "2026-07-17"),   # active yesterday (old account)
        ("garmin", "d@x", "2026-07-16"),   # same account, earlier day → counted once in 7d
        ("whoop",  "c@x", "2026-07-14"),   # inside the week, not yesterday
        ("whoop",  "c@x", "2026-07-10"),   # before the week
    ]
    conn.executemany("INSERT INTO daily_activity (adapter, account_key, day) VALUES (?,?,?)",
                     activity)
    conn.commit()


def test_build_report_counts_on_utc_days():
    conn = store.init_db(":memory:")
    _seed(conn)
    r = report.build_report(conn, NOW)
    assert r["date"] == "2026-07-17"
    assert r["adapters"]["garmin"] == {"new": 1, "active": 2, "total": 3,
                                       "new_week": 1, "active_week": 2}
    assert r["adapters"]["whoop"] == {"new": 0, "active": 0, "total": 1,
                                      "new_week": 1, "active_week": 1}
    assert r["totals"] == {"new": 1, "active": 2, "total": 4, "new_week": 2, "active_week": 3}


def test_now_in_another_zone_still_reports_the_utc_day():
    # 01:30 Prague on the 18th is 23:30 UTC on the 17th → yesterday is the 16th.
    conn = store.init_db(":memory:")
    _seed(conn)
    r = report.build_report(conn, datetime(2026, 7, 18, 1, 30, tzinfo=ZoneInfo("Europe/Prague")))
    assert r["date"] == "2026-07-16"
    assert r["adapters"]["garmin"]["active"] == 1


def test_active_is_exact_for_a_past_day():
    """Regression: activity used to be read from tool_usage.last_used, which only
    keeps the latest call per tool — calling the same tool again today erased
    yesterday's activity, so the report was a lower bound (2026-11-06: 2,022
    reported vs 2,663 in PostHog)."""
    conn = store.init_db(":memory:")
    store.upsert_account(conn, "garmin", "me@x", "{}", "s" * 40)
    conn.execute("INSERT INTO daily_activity VALUES ('garmin', 'me@x', '2026-07-17')")
    conn.execute("INSERT INTO tool_usage VALUES ('garmin','me@x','get_x',5,'2026-07-18 08:00:00')")
    conn.commit()
    assert report.build_report(conn, NOW)["adapters"]["garmin"]["active"] == 1


def test_render_text_has_date_and_totals():
    conn = store.init_db(":memory:")
    _seed(conn)
    text = report.render_text(report.build_report(conn, NOW))
    assert "daily report for 2026-07-17 (UTC)" in text
    assert "Garmin: +1 new · 2 active · 3 total  _(7d: +1 new · 2 active)_" in text
    assert "Total: +1 new · 2 active · 4 users*  _(7d: +2 new · 3 active)_" in text


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
