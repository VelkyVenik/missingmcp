"""Daily user-stats report: yesterday's new / active / total users per connector,
computed straight from the DB. Backs `scripts/daily_report.py` (`sudo mmcp report`
on hydra), which the operator's Grok bot reads for its daily status. Nothing in
the app posts it anywhere — the in-app Slack post was removed 2026-11-07.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from . import store

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


def render_text(report: dict) -> str:
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


def open_ro(db_path: str) -> sqlite3.Connection:
    """Read-only connection for a report run (own connection, thread-safe use).
    Build the file: URI via as_uri() so a path with `?`/`#`/spaces is escaped and
    can't be mis-parsed into a different file or mode."""
    uri = Path(db_path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)
