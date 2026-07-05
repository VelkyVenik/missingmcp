#!/usr/bin/env python3
"""Per-account tool/method usage for the Garmin MCP Gateway (reads the DB read-only).

Shows how many MCP calls each connected account made and which tools/methods they
used. Only names + counts are recorded — never request contents or Garmin data.
Counts include MCP protocol methods (initialize, tools/list, …) that Claude calls
automatically, alongside actual tool calls (get_activities, …).

Usage:
  python scripts/usage.py                  # per-account summary + top tools
  python scripts/usage.py --tools          # overall tool/method leaderboard
  python scripts/usage.py --account me@x   # per-tool breakdown for one account

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
In Docker: docker compose exec gateway python /app/scripts/usage.py
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys


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
    p = argparse.ArgumentParser(description="Garmin MCP Gateway usage metrics.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--account", help="show per-tool breakdown for this account")
    p.add_argument("--tools", action="store_true", help="overall tool/method leaderboard")
    p.add_argument("--limit", type=int, default=15, help="rows in leaderboards (default 15)")
    args = p.parse_args()

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    if db.execute("SELECT COUNT(*) FROM tool_usage").fetchone()[0] == 0:
        print("No usage recorded yet.")
        return

    if args.account:
        key = args.account.strip().lower()
        rows = db.execute(
            "SELECT tool, calls, last_used FROM tool_usage WHERE account_key=? "
            "ORDER BY calls DESC",
            (key,),
        ).fetchall()
        if not rows:
            print(f"No usage recorded for {key}.")
            return
        print(f"\nUsage for {key}\n")
        for r in rows:
            print(f"  {r['tool']:<28} {r['calls']:>5}   last: {r['last_used']}")
        print()
        return

    if args.tools:
        rows = db.execute(
            "SELECT tool, SUM(calls) AS n, COUNT(DISTINCT account_key) AS users "
            "FROM tool_usage GROUP BY tool ORDER BY n DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        print("\nTop tools / methods (all accounts)\n")
        for r in rows:
            print(f"  {r['tool']:<28} {r['n']:>6}   by {r['users']} user(s)")
        print()
        return

    # default: per-account summary + top tools
    print(f"\nGarmin MCP Gateway — usage  ({db_path})\n")
    print("Per account")
    for r in db.execute(
        "SELECT account_key AS key, SUM(calls) AS calls, "
        "COUNT(DISTINCT tool) AS tools, MAX(last_used) AS last "
        "FROM tool_usage GROUP BY account_key ORDER BY calls DESC"
    ).fetchall():
        print(f"  {r['key']:<30} calls: {r['calls']:<6} tools: {r['tools']:<4} last: {r['last']}")

    print("\nTop tools / methods")
    for r in db.execute(
        "SELECT tool, SUM(calls) AS n FROM tool_usage GROUP BY tool ORDER BY n DESC LIMIT ?",
        (args.limit,),
    ).fetchall():
        print(f"  {r['tool']:<28} {r['n']:>6}")
    print()


if __name__ == "__main__":
    main()
