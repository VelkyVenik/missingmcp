"""Daily user-stats report to Slack: yesterday's new / active / total users per
connector, computed straight from the DB and posted once a day from the app
lifespan loop. The same build_report / render_slack functions back the standalone
scripts/daily_report.py, so testing the script tests the real output.

Dependency-free (an httpx POST), mirroring backup.py's shape: a `DailyReport`
class with enabled/due/run, whose run() never raises. DB reads happen against a
fresh read-only connection opened inside run() (never the app's shared conn — run()
executes in a worker thread via asyncio.to_thread, like Backup.run).
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from . import store
from .log import log, log_exc

_UTC_FMT = "%Y-%m-%d %H:%M:%S"   # matches SQLite datetime('now')


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_UTC_FMT)


def _yesterday_bounds(now_local: datetime):
    """UTC bounds of the local calendar day before now_local, plus the local
    start-of-yesterday (for the weekly window). Returns (start_utc, end_utc,
    start_local)."""
    today0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = today0 - timedelta(days=1)
    return _utc(start_local), _utc(today0), start_local


def build_report(conn, now_local: datetime) -> dict:
    """Per-adapter new / active / total for yesterday (local calendar day) plus a
    7-day new-account count, computed from `conn`. `now_local` must be tz-aware."""
    start_utc, end_utc, start_local = _yesterday_bounds(now_local)
    week_start_utc = _utc(start_local - timedelta(days=6))  # 7 days incl. yesterday
    new = store.new_accounts_between(conn, start_utc, end_utc)
    active = store.active_accounts_between(conn, start_utc, end_utc)
    total = store.total_accounts_by_adapter(conn)
    new_week = store.new_accounts_between(conn, week_start_utc, end_utc)
    adapters = sorted(set(new) | set(active) | set(total) | set(new_week))
    per = {a: {"new": new.get(a, 0), "active": active.get(a, 0),
               "total": total.get(a, 0), "new_week": new_week.get(a, 0)}
           for a in adapters}
    return {
        "date": start_local.strftime("%Y-%m-%d"),
        "adapters": per,
        "totals": {k: sum(v[k] for v in per.values())
                   for k in ("new", "active", "total", "new_week")},
    }


def render_slack(report: dict) -> str:
    t = report["totals"]
    lines = [f"*MissingMCP — daily report for {report['date']}*"]
    if report["adapters"]:
        for a, v in report["adapters"].items():
            lines.append(
                f"• {a.capitalize()}: +{v['new']} new · {v['active']} active "
                f"· {v['total']} total  _(7d: +{v['new_week']} new)_")
    else:
        lines.append("_no connectors with accounts yet_")
    lines.append(
        f"*Total: +{t['new']} new · {t['active']} active · {t['total']} users*  "
        f"_(7d: +{t['new_week']} new)_")
    return "\n".join(lines)


def post_slack(webhook_url: str, text: str) -> None:
    """Blocking POST of one Slack message. Raises on failure."""
    resp = httpx.post(webhook_url, json={"text": text}, timeout=15.0)
    if resp.status_code != 200:
        raise RuntimeError(f"slack post rejected: HTTP {resp.status_code}")


def open_ro(db_path: str) -> sqlite3.Connection:
    """Read-only connection for a report run (own connection, thread-safe use).
    Build the file: URI via as_uri() so a path with `?`/`#`/spaces is escaped and
    can't be mis-parsed into a different file or mode."""
    uri = Path(db_path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


class DailyReport:
    """Posts the daily user-stats report to Slack once a day at ~report hour (local
    tz), driven from the app lifespan loop. run() never raises. Process-local: a
    redeploy AFTER the report hour skips today and resumes tomorrow, so frequent
    deploys don't produce duplicate posts (at the cost of not catching up if the
    app was down for the whole report hour)."""

    def __init__(self, config, now: datetime | None = None):
        self._cfg = config
        self._tz = ZoneInfo(config.daily_report_tz)
        n = now or datetime.now(self._tz)
        self._last_date = n.date() if n.hour >= config.daily_report_hour else None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.slack_webhook_url)

    def due(self, now: datetime | None = None) -> bool:
        n = now or datetime.now(self._tz)
        return n.hour >= self._cfg.daily_report_hour and n.date() != self._last_date

    def run(self, now: datetime | None = None) -> None:   # blocking: via asyncio.to_thread
        n = now or datetime.now(self._tz)
        try:
            conn = open_ro(self._cfg.db_path)
            try:
                report = build_report(conn, n)
            finally:
                conn.close()
            post_slack(self._cfg.slack_webhook_url, render_slack(report))
            log("daily-report-ok", date=report["date"], **report["totals"])
        except Exception as e:  # noqa: BLE001 - a report must never take the gateway down
            log_exc("daily-report-failed", e, error=str(e))
        finally:
            self._last_date = n.date()
