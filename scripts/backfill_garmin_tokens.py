#!/usr/bin/env python3
"""One-off backfill: persist worker-rotated Garmin token files into the store.

Before the read-back fix (PR #15), `materialize()` was write-only: the worker
(garth) rotated tokens into `<DATA_DIR>/users/<key>/tokens/garmin_tokens.json`
and the store never learned — on 2026-07-27, 84 of 168 garmin accounts' files
were AHEAD of their DB blob, so the next spawn replayed a spent refresh token
and forced a re-login. The fix stops new drift, but a fresh gateway process
deliberately trusts the store over the disk (unknown provenance), so pre-fix
drift is repaired only by this explicit, operator-run backfill
(.scratch/reliability ticket 05).

A file is persisted into the store only when ALL of:
  - it exists and parses as JSON (a torn write is never persisted),
  - its content differs from the decrypted DB blob,
  - its mtime is NEWER than the account's `updated_at` — a re-login that
    happened after the file was written must win (the same "store wins on
    unknown provenance" rule the gateway itself follows after a restart).

SAFE BY DEFAULT — dry-run prints aggregate counts and masked account keys,
persists nothing. Pass --apply to persist. Safe to run while the gateway is
live: WAL + short upserts (busy_timeout set); a concurrent materialize can
only make a row read as in-sync or torn, both of which are skipped. Re-running
is idempotent — a persisted row reads as in-sync next time.

Usage (production):
  railway ssh --service gateway "python3 /app/scripts/backfill_garmin_tokens.py"          # dry-run
  railway ssh --service gateway "python3 /app/scripts/backfill_garmin_tokens.py --apply"
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Make `garmin` importable from a checkout (src/ layout), not only installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from garmin import store                     # noqa: E402
from garmin.workers import _SAFE             # noqa: E402 - the one owner of key->dirname


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def _updated_at_epoch(s: str) -> float:
    """SQLite datetime('now') string ("YYYY-MM-DD HH:MM:SS", UTC) -> epoch."""
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def classify(data_dir: str, key: str, blob: str, updated_at: str) -> str:
    """One account's verdict: no-file | torn | in-sync | db-newer | drifted."""
    path = os.path.join(data_dir, "users", _SAFE.sub("_", key),
                        "tokens", "garmin_tokens.json")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return "no-file"
    try:
        json.loads(content)
    except ValueError:
        return "torn"
    if content == blob:
        return "in-sync"
    if os.stat(path).st_mtime <= _updated_at_epoch(updated_at):
        return "db-newer"
    return "drifted"


def read_file(data_dir: str, key: str) -> str:
    path = os.path.join(data_dir, "users", _SAFE.sub("_", key),
                        "tokens", "garmin_tokens.json")
    with open(path, encoding="utf-8") as f:
        return f.read()


def backfill(conn, data_dir: str, secret: str, apply: bool = False) -> dict:
    """Classify every garmin account; when `apply`, persist the drifted files.
    Returns {verdict: [masked keys]} for reporting (aggregates + masks only —
    per-account detail stays in the DB)."""
    rows = conn.execute(
        "SELECT account_key, blob_enc, updated_at FROM accounts "
        "WHERE adapter='garmin' ORDER BY account_key"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        key = r["account_key"]
        blob = store.decrypt(secret, r["blob_enc"])
        verdict = classify(data_dir, key, blob, r["updated_at"])
        if verdict == "drifted" and apply:
            # Re-read inside the persist step: classify() proved it parses; a
            # rewrite in between just lands us on the newer content, which is
            # equally the worker's latest word.
            content = read_file(data_dir, key)
            try:
                json.loads(content)
            except ValueError:
                verdict = "torn"                 # went torn between the reads
            else:
                store.upsert_account(conn, "garmin", key, content, secret)
                verdict = "persisted"
        out.setdefault(verdict, []).append(key[:3] + "***")
    return out


def main():
    p = argparse.ArgumentParser(
        description="Persist worker-rotated Garmin token files into the store.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--data-dir", default=None,
                   help="DATA_DIR holding users/<key>/tokens (default: the DB's directory)")
    p.add_argument("--apply", action="store_true",
                   help="actually persist (default: dry-run, writes nothing)")
    args = p.parse_args()

    secret = os.environ.get("GATEWAY_SECRET", "")
    if not secret:
        sys.exit("GATEWAY_SECRET not set — needed to decrypt blobs for comparison.")
    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")
    data_dir = args.data_dir or os.environ.get("DATA_DIR") or os.path.dirname(db_path)

    conn = store.init_db(db_path)
    conn.execute("PRAGMA busy_timeout=5000")     # the live gateway shares this DB
    try:
        result = backfill(conn, data_dir, secret, apply=args.apply)
    finally:
        conn.close()

    mode = "APPLIED" if args.apply else "DRY-RUN (nothing written — pass --apply)"
    print(f"garmin token backfill — {mode}")
    order = ["persisted", "drifted", "in-sync", "db-newer", "torn", "no-file"]
    for verdict in order:
        keys = result.get(verdict, [])
        if not keys:
            continue
        line = f"  {verdict:10} {len(keys):4}"
        if verdict in ("drifted", "persisted", "db-newer", "torn"):
            line += "   " + " ".join(keys)
        print(line)
    total = sum(len(v) for v in result.values())
    print(f"  total: {total} garmin account(s)")


if __name__ == "__main__":
    main()
