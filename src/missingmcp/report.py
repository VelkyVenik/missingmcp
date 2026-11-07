"""Daily user-stats report: yesterday's new / active / total users per connector,
plus 7-day new and active counts, computed straight from the DB. Backs
`scripts/daily_report.py` (`sudo mmcp report` on hydra), which the operator's
Grok bot reads for its daily status.

Days are UTC calendar days — the same days PostHog's `mcp_active` uses — because
`daily_activity` buckets activity by UTC day.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import store

_UTC_FMT = "%Y-%m-%d %H:%M:%S"   # matches SQLite datetime('now')


def build_report(conn, now: datetime) -> dict:
    """Per-adapter counts for the UTC day before `now` (tz-aware): new accounts,
    accounts that invoked a tool, total accounts, and the 7-day (incl. that day)
    new and active counts."""
    today0 = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day0 = today0 - timedelta(days=1)
    week0 = day0 - timedelta(days=6)
    day, week_first = day0.strftime("%Y-%m-%d"), week0.strftime("%Y-%m-%d")
    end = today0.strftime(_UTC_FMT)
    new = store.new_accounts_between(conn, day0.strftime(_UTC_FMT), end)
    new_week = store.new_accounts_between(conn, week0.strftime(_UTC_FMT), end)
    active = store.active_accounts_on_days(conn, day, day)
    active_week = store.active_accounts_on_days(conn, week_first, day)
    total = store.total_accounts_by_adapter(conn)
    adapters = sorted(set(new) | set(active) | set(total) | set(new_week) | set(active_week))
    keys = ("new", "active", "total", "new_week", "active_week")
    src = dict(zip(keys, (new, active, total, new_week, active_week)))
    per = {a: {k: src[k].get(a, 0) for k in keys} for a in adapters}
    return {
        "date": day,
        "adapters": per,
        "totals": {k: sum(v[k] for v in per.values()) for k in keys},
    }


def render_text(report: dict) -> str:
    t = report["totals"]
    lines = [f"*MissingMCP — daily report for {report['date']} (UTC)*"]
    if report["adapters"]:
        for a, v in report["adapters"].items():
            lines.append(
                f"• {a.capitalize()}: +{v['new']} new · {v['active']} active "
                f"· {v['total']} total  _(7d: +{v['new_week']} new · {v['active_week']} active)_")
    else:
        lines.append("_no connectors with accounts yet_")
    lines.append(
        f"*Total: +{t['new']} new · {t['active']} active · {t['total']} users*  "
        f"_(7d: +{t['new_week']} new · {t['active_week']} active)_")
    return "\n".join(lines)


def open_ro(db_path: str) -> sqlite3.Connection:
    """Read-only connection for a report run (own connection, thread-safe use).
    Build the file: URI via as_uri() so a path with `?`/`#`/spaces is escaped and
    can't be mis-parsed into a different file or mode."""
    uri = Path(db_path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)
