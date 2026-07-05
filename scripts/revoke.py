#!/usr/bin/env python3
"""Revoke Garmin MCP Gateway access tokens — a kill-switch for a leaked/lost token.

Usage:
  python scripts/revoke.py --list                  # accounts + token counts
  python scripts/revoke.py --account me@x.cz        # revoke ALL tokens for an account
  python scripts/revoke.py --token-hash <sha256>    # revoke one token by its hash

Removes only the bearer access tokens; the account's stored Garmin tokens are
left intact, so the user simply re-authenticates in Claude. The running gateway
re-checks the DB on every request, so revocation takes effect immediately.

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
In Docker: docker compose exec gateway python /app/scripts/revoke.py --account <email>
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
    p = argparse.ArgumentParser(description="Revoke gateway access tokens.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list accounts and token counts")
    g.add_argument("--account", help="revoke ALL tokens for this account (email)")
    g.add_argument("--token-hash", help="revoke a single token by its SHA-256 hash")
    args = p.parse_args()

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if args.list:
        rows = conn.execute(
            "SELECT account_key AS key, adapter, COUNT(*) AS n, MAX(last_used) AS last "
            "FROM access_tokens GROUP BY adapter, account_key ORDER BY adapter, account_key"
        ).fetchall()
        if not rows:
            print("No access tokens.")
        for r in rows:
            print(f"  {r['adapter']}:{r['key']:<32} tokens: {r['n']:<3} last used: {r['last'] or '—'}")
        return

    if args.account:
        key = args.account.strip().lower()  # stored lowercased, as in _finish
        cur = conn.execute("DELETE FROM access_tokens WHERE account_key=?", (key,))
        conn.commit()
        print(f"Revoked {cur.rowcount} token(s) for {key}. They must reconnect in Claude.")
        return

    cur = conn.execute("DELETE FROM access_tokens WHERE token_hash=?", (args.token_hash,))
    conn.commit()
    print(f"Revoked {cur.rowcount} token(s).")


if __name__ == "__main__":
    main()
