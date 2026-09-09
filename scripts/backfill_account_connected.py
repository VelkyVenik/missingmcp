#!/usr/bin/env python3
"""One-off backfill of `account_connected` events into PostHog from the DB.

PostHog telemetry is forward-only by design, so connects that happened before
telemetry went live are missing. This replays them from the durable record: for
each row in `accounts` it emits one `account_connected` event, backdated to the
account's `created_at` and keyed by the login email (distinct_id) — the same
identity and shape the live gateway emits. `connect_status` is "new" (the
historical new/returning split can't be reconstructed) and `backfill: true`
marks these apart from organic events. Each event carries a deterministic uuid
derived from (adapter, account_key), so PostHog dedupes re-sends and re-running
is idempotent.

SAFE BY DEFAULT — prints what it *would* send (dry-run). Pass --apply to emit.
Use --before to cover only accounts created before telemetry go-live, so you
don't double-count connects the live gateway already captured.

Usage:
  python scripts/backfill_account_connected.py                        # dry-run, all accounts
  python scripts/backfill_account_connected.py --before 2026-07-20    # dry-run, pre-go-live only
  python scripts/backfill_account_connected.py --before 2026-07-20 --apply   # actually emit

DB path resolves like daily_report.py ($DB_PATH, $DATA_DIR/gateway.db, /data,
./.localdata). Dry-run needs only the DB; --apply also needs GATEWAY_SECRET (to
build config) and POSTHOG_API_KEY (to emit) — both present on Railway, where
this runs:
  railway ssh --service gateway "python3 /app/scripts/backfill_account_connected.py --before 2026-07-20 --apply"
"""
from __future__ import annotations
import argparse
import os
import sys
import uuid as uuidlib
from datetime import datetime, timezone

# Make `garmin` importable from a checkout (src/ layout), not only installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from garmin import store, telemetry          # noqa: E402
from garmin.config import load_config        # noqa: E402

# Fixed namespace → the per-account event uuid is stable across runs (idempotent).
_NS = uuidlib.UUID("a1b2c3d4-e5f6-4a7b-8c9d-000000000001")


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def _parse_dt(s: str) -> datetime:
    """Parse a SQLite `datetime('now')` string ("YYYY-MM-DD HH:MM:SS", UTC).
    Python 3.11+ fromisoformat accepts the space separator; naive → UTC."""
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def event_uuid(adapter: str, account_key: str) -> str:
    """Deterministic event id for one account's backfilled connect."""
    return str(uuidlib.uuid5(_NS, f"account_connected:backfill:{adapter}:{account_key}"))


def load_accounts(conn, before: datetime | None = None) -> list[dict]:
    """Accounts oldest-first, optionally only those created before `before`."""
    rows = conn.execute(
        "SELECT adapter, account_key, created_at FROM accounts ORDER BY created_at"
    ).fetchall()
    out = []
    for r in rows:
        created = _parse_dt(r["created_at"])
        if before is not None and created >= before:
            continue
        out.append({"adapter": r["adapter"], "account_key": r["account_key"],
                    "created_at": created})
    return out


def backfill(conn, *, before: datetime | None = None, apply: bool = False) -> list[dict]:
    """Return the accounts in scope; when `apply`, emit one `account_connected`
    per account (backdated, deterministic uuid). Dry-run emits nothing."""
    accounts = load_accounts(conn, before)
    if apply:
        for a in accounts:
            telemetry.capture(
                "account_connected", distinct_id=a["account_key"],
                properties={"adapter": a["adapter"], "connect_status": "new",
                            "backfill": True},
                timestamp=a["created_at"],
                uuid=event_uuid(a["adapter"], a["account_key"]),
            )
    return accounts


def main():
    p = argparse.ArgumentParser(description="Backfill account_connected from the DB.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--before", default=None, metavar="YYYY-MM-DD",
                   help="only accounts created before this date (avoid double-counting "
                        "connects the live telemetry already captured)")
    p.add_argument("--apply", action="store_true",
                   help="actually emit to PostHog (default: dry-run, emits nothing)")
    args = p.parse_args()

    before = None
    if args.before:
        try:
            before = datetime.strptime(args.before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit(f"--before must be YYYY-MM-DD, got: {args.before!r}")

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    if args.apply:
        telemetry.init(load_config(os.environ))    # no-op without POSTHOG_API_KEY
        if not telemetry.enabled():
            sys.exit("POSTHOG_API_KEY not set — --apply would emit nothing. Aborting.")

    conn = store.init_db(db_path)
    try:
        accounts = backfill(conn, before=before, apply=args.apply)
    finally:
        conn.close()
        if args.apply:
            telemetry.shutdown()                   # flush the queue before exit

    mode = "EMITTED" if args.apply else "DRY-RUN (nothing sent — pass --apply to emit)"
    print(f"account_connected backfill — {mode}")
    if args.before:
        print(f"  filter: created_at < {args.before}")
    by_adapter: dict[str, int] = {}
    for a in accounts:
        by_adapter[a["adapter"]] = by_adapter.get(a["adapter"], 0) + 1
        print(f"  {a['created_at'].strftime('%Y-%m-%d')}  {a['adapter']:8}  {a['account_key']}")
    print(f"  total: {len(accounts)} account(s)" + (f" — {by_adapter}" if by_adapter else ""))


if __name__ == "__main__":
    main()
