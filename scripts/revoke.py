#!/usr/bin/env python3
"""Revoke MCP gateway access tokens — a kill-switch for a leaked/lost token.

Usage:
  python scripts/revoke.py --list                      # accounts + token counts
  python scripts/revoke.py --account me@x.cz           # ALL garmin tokens for an account
  python scripts/revoke.py --account rohlik:me@x.cz    # adapter-scoped
  python scripts/revoke.py --account me@x.cz --purge   # + delete stored account & usage
  python scripts/revoke.py --device ab12cd34           # one device, by token-hash prefix
                                                       #   (prefixes shown by status.py)

Revoking tokens only logs devices out; the account's stored service tokens are
left intact, so the user simply re-authenticates in Claude. --purge also deletes
the encrypted account row and its usage metrics (full off-boarding). The running
gateway re-checks the DB on every request, so revocation takes effect immediately.

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
In Docker:  docker compose exec gateway python /app/scripts/revoke.py --account <email>
On Railway: railway ssh --service gateway "python3 /app/scripts/revoke.py --account <email>"
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import urllib.request

DEFAULT_ADAPTER = "garmin"


def posthog_event(event: str, distinct_id: str, props: dict) -> None:
    """Best-effort one-shot capture (stdlib only — this script stays
    dependency-free). Silent no-op without POSTHOG_API_KEY or on any error:
    revocation must never be blocked by telemetry."""
    key = os.environ.get("POSTHOG_API_KEY", "")
    if not key:
        return
    host = os.environ.get("POSTHOG_HOST", "https://eu.i.posthog.com").rstrip("/")
    payload = json.dumps({"api_key": key, "event": event,
                          "distinct_id": distinct_id, "properties": props}).encode()
    req = urllib.request.Request(f"{host}/i/v0/e/", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:  # noqa: BLE001
        pass


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
    p = argparse.ArgumentParser(description="Revoke gateway access tokens.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--purge", action="store_true",
                   help="with --account: also delete the stored account row + usage metrics")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list accounts and token counts")
    g.add_argument("--account", metavar="[ADAPTER:]KEY",
                   help="revoke ALL tokens for this account (bare key = garmin)")
    g.add_argument("--device", metavar="HASH_PREFIX",
                   help="revoke one device by its token-hash prefix "
                        "(>=8 chars; a full hash works too)")
    args = p.parse_args()
    if args.purge and not args.account:
        p.error("--purge only makes sense with --account")

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
            print(f"  {r['adapter']}:{r['key']:<32} tokens: {r['n']:<3} "
                  f"last used: {r['last'] or '—'}")
        return

    if args.device:
        prefix = args.device.strip().lower().rstrip("…")  # status.py prints "ab12cd34…"
        if len(prefix) < 8:
            sys.exit("Prefix too short — give at least 8 characters (status.py shows them).")
        if not all(c in "0123456789abcdef" for c in prefix):
            sys.exit("Not a token-hash prefix — expected hex characters (status.py shows them).")
        rows = conn.execute(
            "SELECT token_hash, adapter, account_key FROM access_tokens "
            "WHERE token_hash LIKE ?", (prefix + "%",)
        ).fetchall()
        if not rows:
            print(f"No token matches {prefix}…")
            return
        if len(rows) > 1:
            for r in rows:
                print(f"  {r['token_hash'][:16]}…  {r['adapter']}:{r['account_key']}")
            sys.exit(f"Ambiguous prefix — {len(rows)} tokens match; give more characters.")
        conn.execute("DELETE FROM access_tokens WHERE token_hash=?",
                     (rows[0]["token_hash"],))
        conn.commit()
        print(f"Revoked device {prefix}… of "
              f"{rows[0]['adapter']}:{rows[0]['account_key']}.")
        return

    adapter, key = parse_account(args.account)
    cur = conn.execute(
        "DELETE FROM access_tokens WHERE adapter=? AND account_key=?", (adapter, key))
    msg = f"Revoked {cur.rowcount} token(s) for {adapter}:{key}."
    if args.purge:
        conn.execute("DELETE FROM tool_usage WHERE adapter=? AND account_key=?",
                     (adapter, key))
        purged = conn.execute(
            "DELETE FROM accounts WHERE adapter=? AND account_key=?", (adapter, key))
        msg += (" Purged the stored account + usage." if purged.rowcount
                else " No stored account row to purge.")
    else:
        msg += " They must reconnect in Claude."
    conn.commit()
    posthog_event("account_revoked", key, {"adapter": adapter, "purged": bool(args.purge)})
    print(msg)
    if args.purge:
        print("Reminder: also delete the person in PostHog "
              "(EU project → People) to complete the off-boarding.")


if __name__ == "__main__":
    main()
