#!/usr/bin/env python3
"""Daily user-stats report for the MCP gateway (reads the DB read-only).

Yesterday's new / active / total users per connector, plus a 7-day new count
(`missingmcp.report.build_report` / `render_text`), printed to stdout. On hydra
it runs as `sudo mmcp report`, which the Grok bot reads for its daily status.

Usage:
  python scripts/daily_report.py                       # print yesterday's report
  python scripts/daily_report.py --now 2026-07-18      # report the UTC day BEFORE this date

DB path resolves like usage.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
On hydra (/opt/missingmcp): docker compose exec gateway python3 /app/scripts/daily_report.py
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone

# Make `missingmcp` importable when run from a checkout (src/ layout), not only
# when installed (Docker/uv).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from missingmcp import report as report_mod  # noqa: E402


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def main():
    p = argparse.ArgumentParser(description="MCP gateway daily user-stats report.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--now", default=None,
                   help="ISO datetime/date (naive = UTC); the report covers the UTC "
                        "day BEFORE it (default: real now)")
    args = p.parse_args()

    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    conn = report_mod.open_ro(db_path)
    try:
        rep = report_mod.build_report(conn, now)
    finally:
        conn.close()
    print(report_mod.render_text(rep))


if __name__ == "__main__":
    main()
