#!/usr/bin/env python3
"""Status / stats snapshot for the Garmin MCP Gateway (reads the DB read-only).

Shows how many people have a token, how many devices/clients are connected, the
registered OAuth clients, and per-account token counts. Safe to run while the
gateway is live (opens the SQLite DB read-only).

Usage:
  python scripts/status.py                 # uses ./.localdata/gateway.db
  python scripts/status.py --db /data/gateway.db
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys


def resolve_db() -> str:
    """Find the SQLite DB without flags: explicit env first, then the usual
    container (/data) and local-dev (./.localdata) locations."""
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def main():
    p = argparse.ArgumentParser(description="Garmin MCP Gateway status snapshot.")
    p.add_argument("--db", default=None,
                   help="SQLite DB path (default: $DB_PATH, $DATA_DIR/gateway.db, "
                        "/data/gateway.db, or ./.localdata/gateway.db)")
    args = p.parse_args()
    db_path = args.db or resolve_db()

    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731

    accounts = one("SELECT COUNT(*) FROM garmin_accounts")
    tokens = one("SELECT COUNT(*) FROM access_tokens")
    people = one("SELECT COUNT(DISTINCT garmin_user_key) FROM access_tokens")
    clients = one("SELECT COUNT(*) FROM oauth_clients")
    pending = one("SELECT COUNT(*) FROM oauth_codes")

    print(f"\nGarmin MCP Gateway — status  ({db_path})\n")
    print("Summary")
    print(f"  People with a token : {people}")
    print(f"  Access tokens       : {tokens}   (devices/clients connected)")
    print(f"  Garmin accounts     : {accounts}")
    print(f"  OAuth clients       : {clients}   (registered apps)")
    print(f"  Pending auth codes  : {pending}")

    # per-account: token count + last use
    rows = db.execute(
        """
        SELECT a.garmin_user_key AS key, a.created_at AS created,
               COUNT(t.token_hash) AS tokens, MAX(t.last_used) AS last_used
        FROM garmin_accounts a
        LEFT JOIN access_tokens t ON t.garmin_user_key = a.garmin_user_key
        GROUP BY a.garmin_user_key ORDER BY a.created_at
        """
    ).fetchall()
    if rows:
        print("\nAccounts")
        for r in rows:
            print(f"  {r['key']:<32} tokens: {r['tokens']:<3} "
                  f"connected: {r['created']}  last used: {r['last_used'] or '—'}")

    crows = db.execute(
        """
        SELECT c.client_name AS name, c.redirect_uris AS redirect,
               COUNT(t.token_hash) AS tokens,
               GROUP_CONCAT(DISTINCT t.garmin_user_key) AS accounts
        FROM oauth_clients c
        LEFT JOIN access_tokens t ON t.client_id = c.client_id
        GROUP BY c.client_id ORDER BY c.created_at
        """
    ).fetchall()
    if crows:
        print("\nOAuth clients (registered)")
        for r in crows:
            name = r["name"] or "(unnamed)"
            accounts = r["accounts"] or "—  (never completed OAuth)"
            print(f"  {name:<30} account: {accounts:<28} tokens: {r['tokens']:<3} {r['redirect']}")

    # Active runners live in the gateway's memory; it persists a snapshot to
    # workers.json (next to the DB) so we can show them here.
    wpath = os.path.join(os.path.dirname(os.path.abspath(db_path)), "workers.json")
    print("\nActive runners (per-user garmin_mcp workers)")
    if os.path.exists(wpath):
        try:
            snap = json.load(open(wpath, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snap = None
        if snap and snap.get("workers"):
            for w in snap["workers"]:
                state = "alive" if w.get("alive") else "DEAD"
                print(f"  {w['key']:<32} port {w['port']}  pid {w['pid']}  "
                      f"{state}  idle {w['idle_seconds']}s")
            print(f"  (snapshot at {snap.get('updated', '?')})")
        elif snap is not None:
            print(f"  none running   (snapshot at {snap.get('updated', '?')})")
        else:
            print("  (workers.json unreadable)")
    else:
        print("  (no snapshot — gateway not running yet, or a different DATA_DIR)")
    print()


if __name__ == "__main__":
    main()
