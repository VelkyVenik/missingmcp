#!/usr/bin/env python3
"""Per-account tool/method usage for the MCP gateway (reads the DB read-only).

Shows how many MCP calls each connected account made and which tools/methods
they used. Only names + counts are recorded — never request contents or any
service data. Counts include MCP protocol methods (initialize, tools/list, …)
that Claude calls automatically, alongside actual tool calls.

Usage:
  python scripts/usage.py                          # per-account summary + top tools
  python scripts/usage.py --tools                  # overall tool/method leaderboard
  python scripts/usage.py --account me@x           # one garmin account's breakdown
  python scripts/usage.py --account rohlik:me@x    # adapter-scoped

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
In Docker:  docker compose exec gateway python /app/scripts/usage.py
On Railway: railway ssh --service gateway "python3 /app/scripts/usage.py"
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

DEFAULT_ADAPTER = "garmin"


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def parse_account(value: str) -> tuple[str, str]:
    """'rohlik:me@x.cz' -> ('rohlik', 'me@x.cz'); a bare key defaults to garmin.
    Keys are stored lowercased (oauth._finish), so normalize here too."""
    adapter, sep, key = value.partition(":")
    if not sep:
        return DEFAULT_ADAPTER, adapter.strip().lower()
    return adapter.strip().lower(), key.strip().lower()


def main():
    p = argparse.ArgumentParser(description="MCP gateway usage metrics.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--account", metavar="[ADAPTER:]KEY",
                   help="show per-tool breakdown for this account (bare key = garmin)")
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
        adapter, key = parse_account(args.account)
        rows = db.execute(
            "SELECT tool, calls, last_used FROM tool_usage "
            "WHERE adapter=? AND account_key=? ORDER BY calls DESC",
            (adapter, key),
        ).fetchall()
        if not rows:
            print(f"No usage recorded for {adapter}:{key}.")
            return
        print(f"\nUsage for {adapter}:{key}\n")
        for r in rows:
            print(f"  {r['tool']:<28} {r['calls']:>5}   last: {r['last_used']}")
        print()
        return

    if args.tools:
        rows = db.execute(
            "SELECT adapter, tool, SUM(calls) AS n, COUNT(DISTINCT account_key) AS users "
            "FROM tool_usage GROUP BY adapter, tool ORDER BY n DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        print("\nTop tools / methods (all accounts)\n")
        for r in rows:
            label = f"{r['adapter']}:{r['tool']}"
            print(f"  {label:<36} {r['n']:>6}   by {r['users']} user(s)")
        print()
        return

    # default: per-account summary + top tools
    print(f"\nMissingMCP gateway — usage  ({db_path})\n")
    print("Per account")
    for r in db.execute(
        "SELECT adapter, account_key AS key, SUM(calls) AS calls, "
        "COUNT(DISTINCT tool) AS tools, MAX(last_used) AS last "
        "FROM tool_usage GROUP BY adapter, account_key ORDER BY calls DESC"
    ).fetchall():
        acct = f"{r['adapter']}:{r['key']}"
        print(f"  {acct:<38} calls: {r['calls']:<6} tools: {r['tools']:<4} last: {r['last']}")

    print("\nTop tools / methods")
    for r in db.execute(
        "SELECT adapter, tool, SUM(calls) AS n FROM tool_usage "
        "GROUP BY adapter, tool ORDER BY n DESC LIMIT ?",
        (args.limit,),
    ).fetchall():
        label = f"{r['adapter']}:{r['tool']}"
        print(f"  {label:<36} {r['n']:>6}")
    print()


if __name__ == "__main__":
    main()
