#!/usr/bin/env python3
"""Status / stats snapshot for the MCP gateway (reads the DB read-only).

One overview: every connected account (adapter:key) with its devices —
token-hash prefixes you can pass to revoke.py --device — plus a tool-usage
summary, the registered OAuth clients, and the running workers. Safe to run
while the gateway is live (opens the SQLite DB read-only).

Usage:
  python scripts/status.py                 # uses ./.localdata/gateway.db
  python scripts/status.py --db /data/gateway.db
  railway ssh --service gateway "python3 /app/scripts/status.py"
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
    p = argparse.ArgumentParser(description="MCP gateway status snapshot.")
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

    accounts = one("SELECT COUNT(*) FROM accounts")
    tokens = one("SELECT COUNT(*) FROM access_tokens")
    people = one("SELECT COUNT(DISTINCT adapter || ':' || account_key) FROM access_tokens")
    clients = one("SELECT COUNT(*) FROM oauth_clients")
    pending = one("SELECT COUNT(*) FROM oauth_codes")

    print(f"\nMissingMCP gateway — status  ({db_path})\n")
    print("Summary")
    print(f"  People with a token : {people}")
    print(f"  Access tokens       : {tokens}   (devices/clients connected)")
    print(f"  Accounts            : {accounts}")
    print(f"  OAuth clients       : {clients}   (registered apps)")
    print(f"  Pending auth codes  : {pending}")

    client_names = {c["client_id"]: (c["client_name"] or "(unnamed)")
                    for c in db.execute("SELECT client_id, client_name FROM oauth_clients")}
    tokens_by_acct: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for t in db.execute(
        "SELECT adapter, account_key, token_hash, client_id, created_at, last_used "
        "FROM access_tokens ORDER BY created_at"
    ):
        tokens_by_acct.setdefault((t["adapter"], t["account_key"]), []).append(t)
    usage_by_acct = {
        (u["adapter"], u["account_key"]): u
        for u in db.execute(
            "SELECT adapter, account_key, SUM(calls) AS calls, COUNT(*) AS tools, "
            "MAX(last_used) AS last FROM tool_usage GROUP BY adapter, account_key"
        )
    }

    rows = db.execute(
        "SELECT adapter, account_key, created_at FROM accounts ORDER BY created_at"
    ).fetchall()
    if rows:
        print("\nAccounts")
        for a in rows:
            k = (a["adapter"], a["account_key"])
            devices = tokens_by_acct.pop(k, [])
            last = max((t["last_used"] or "" for t in devices), default="") or "—"
            print(f"  {a['adapter']}:{a['account_key']:<32} devices: {len(devices):<3} "
                  f"connected: {a['created_at']}  last used: {last}")
            u = usage_by_acct.get(k)
            if u:
                print(f"    usage: calls: {u['calls']} across {u['tools']} tool(s), "
                      f"last {u['last']}")
            for t in devices:
                print(f"    {t['token_hash'][:8]}…  "
                      f"{client_names.get(t['client_id'], '?'):<24} "
                      f"created: {t['created_at']}  last used: {t['last_used'] or '—'}")
    if tokens_by_acct:  # tokens whose account row is gone (e.g. after --purge)
        print("\nTokens without a stored account (revoke these)")
        for (adapter, key), devices in tokens_by_acct.items():
            for t in devices:
                print(f"  {t['token_hash'][:8]}…  {adapter}:{key}")

    crows = db.execute(
        """
        SELECT c.adapter AS adapter, c.client_name AS name, c.redirect_uris AS redirect,
               COUNT(t.token_hash) AS tokens,
               GROUP_CONCAT(DISTINCT t.account_key) AS accounts
        FROM oauth_clients c
        LEFT JOIN access_tokens t ON t.client_id = c.client_id
        GROUP BY c.client_id ORDER BY c.created_at
        """
    ).fetchall()
    if crows:
        print("\nOAuth clients (registered)")
        for r in crows:
            name = r["name"] or "(unnamed)"
            accts = r["accounts"] or "—  (never completed OAuth)"
            print(f"  [{r['adapter']}] {name:<28} account: {accts:<28} "
                  f"tokens: {r['tokens']:<3} {r['redirect']}")

    # Active runners live in the gateway's memory; it persists a snapshot to
    # workers.json (next to the DB) so we can show them here.
    wpath = os.path.join(os.path.dirname(os.path.abspath(db_path)), "workers.json")
    print("\nActive runners (per-account MCP workers)")
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
