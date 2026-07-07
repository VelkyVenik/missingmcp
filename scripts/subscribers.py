#!/usr/bin/env python3
"""List newsletter subscribers and connector suggestions (reads the DB read-only).

No email is sent from here — this is capture-only until a sending provider is
wired up. Use --emails to get a plain list to paste into an email tool.

Usage:
  python scripts/subscribers.py                 # table view (counts + rows)
  python scripts/subscribers.py --emails        # subscriber emails, one per line
  python scripts/subscribers.py --db /data/gateway.db
  railway ssh --service gateway "python3 /app/scripts/subscribers.py --emails"
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


def _rows(db, sql):
    # Tolerate a DB the gateway hasn't opened since this feature shipped.
    try:
        return db.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def main():
    p = argparse.ArgumentParser(description="MissingMCP subscribers & suggestions.")
    p.add_argument("--db", default=None,
                   help="SQLite DB path (default: $DB_PATH, $DATA_DIR/gateway.db, "
                        "/data/gateway.db, or ./.localdata/gateway.db)")
    p.add_argument("--emails", action="store_true",
                   help="print only subscriber emails, one per line")
    args = p.parse_args()
    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    subs = _rows(db, "SELECT email, created_at FROM subscribers ORDER BY created_at")
    if args.emails:
        for s in subs:
            print(s["email"])
        return

    print(f"\nMissingMCP — subscribers & suggestions  ({db_path})\n")
    print(f"Newsletter subscribers: {len(subs)}")
    for s in subs:
        print(f"  {s['email']:<40} since {s['created_at']}")

    sugg = _rows(db, "SELECT email, description, wants_updates, created_at "
                     "FROM suggestions ORDER BY created_at")
    print(f"\nConnector suggestions: {len(sugg)}")
    for s in sugg:
        flag = " (+updates)" if s["wants_updates"] else ""
        print(f"  {s['created_at']}  {s['email']}{flag}")
        desc = (s["description"] or "").replace("\n", " ").strip()
        if desc:
            print(f"      {desc}")
    print()


if __name__ == "__main__":
    main()
