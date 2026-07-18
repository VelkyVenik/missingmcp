"""Daily user-stats report — DB metrics, timezone boundaries, and the DailyReport
scheduler. Everything is deterministic: `now` is injected, so no wall-clock reads."""
from datetime import datetime
from zoneinfo import ZoneInfo

from missingmcp import report, store
from missingmcp.config import load_config

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


def _cfg(tmp_path, webhook="https://hooks.example.com/x", hour=8):
    env = {"GATEWAY_SECRET": "s" * 40, "DATA_DIR": str(tmp_path),
           "DB_PATH": str(tmp_path / "gateway.db"), "DAILY_REPORT_HOUR": str(hour)}
    if webhook is not None:
        env["SLACK_WEBHOOK_URL"] = webhook
    return load_config(env)


def test_build_report_counts_and_tz_boundary():
    conn = store.init_db(":memory:")
    _seed(conn)
    r = report.build_report(conn, NOW)
    assert r["date"] == "2026-07-17"
    assert r["adapters"]["garmin"] == {"new": 1, "active": 2, "total": 3, "new_week": 1}
    assert r["adapters"]["whoop"] == {"new": 0, "active": 0, "total": 1, "new_week": 1}
    assert r["totals"] == {"new": 1, "active": 2, "total": 4, "new_week": 2}


def test_render_slack_has_date_and_totals():
    conn = store.init_db(":memory:")
    _seed(conn)
    text = report.render_slack(report.build_report(conn, NOW))
    assert "daily report for 2026-07-17" in text
    assert "Garmin: +1 new · 2 active · 3 total" in text
    assert "Total: +1 new · 2 active · 4 users" in text


def test_render_slack_empty_db():
    conn = store.init_db(":memory:")
    text = report.render_slack(report.build_report(conn, NOW))
    assert "Total: +0 new · 0 active · 0 users" in text  # no crash on empty


def test_enabled_gated_on_webhook(tmp_path):
    assert report.DailyReport(_cfg(tmp_path), now=NOW).enabled is True
    assert report.DailyReport(_cfg(tmp_path, webhook=None), now=NOW).enabled is False


def test_due_fires_once_per_day_after_the_hour(tmp_path):
    cfg = _cfg(tmp_path, hour=8)
    dr = report.DailyReport(cfg, now=datetime(2026, 7, 18, 7, 0, tzinfo=PRAGUE))
    assert dr.due(datetime(2026, 7, 18, 7, 30, tzinfo=PRAGUE)) is False   # before the hour
    assert dr.due(datetime(2026, 7, 18, 8, 30, tzinfo=PRAGUE)) is True    # at/after the hour
    dr._last_date = datetime(2026, 7, 18, 8, 30, tzinfo=PRAGUE).date()    # simulate a run
    assert dr.due(datetime(2026, 7, 18, 20, 0, tzinfo=PRAGUE)) is False   # not again today
    assert dr.due(datetime(2026, 7, 19, 8, 30, tzinfo=PRAGUE)) is True    # next day


def test_redeploy_after_hour_does_not_repost(tmp_path):
    # Constructed at 14:00 (after the 08:00 hour) → today already counts as done.
    dr = report.DailyReport(_cfg(tmp_path), now=datetime(2026, 7, 18, 14, 0, tzinfo=PRAGUE))
    assert dr.due(datetime(2026, 7, 18, 14, 30, tzinfo=PRAGUE)) is False
    assert dr.due(datetime(2026, 7, 19, 8, 30, tzinfo=PRAGUE)) is True


def test_run_posts_and_advances(tmp_path, monkeypatch):
    conn = store.init_db(str(tmp_path / "gateway.db"))
    _seed(conn)
    cfg = _cfg(tmp_path)
    posted = {}
    monkeypatch.setattr(report, "post_slack",
                        lambda url, text: posted.update(url=url, text=text))
    dr = report.DailyReport(cfg, now=datetime(2026, 7, 18, 7, 0, tzinfo=PRAGUE))
    dr.run(now=NOW)
    assert posted["url"] == cfg.slack_webhook_url
    assert "daily report for 2026-07-17" in posted["text"]
    assert dr._last_date == NOW.date()        # advanced → won't repost today


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


def test_run_never_raises_and_still_advances(tmp_path, monkeypatch):
    store.init_db(str(tmp_path / "gateway.db"))
    cfg = _cfg(tmp_path)

    def boom(url, text):
        raise RuntimeError("slack down")
    monkeypatch.setattr(report, "post_slack", boom)
    dr = report.DailyReport(cfg, now=datetime(2026, 7, 18, 7, 0, tzinfo=PRAGUE))
    dr.run(now=NOW)                            # must NOT raise
    assert dr._last_date == NOW.date()
