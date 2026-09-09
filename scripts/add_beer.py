#!/usr/bin/env python3
"""Record a "buy me a beer" donation and emit the beer_purchased event.

Interim manual writer for the beer-supporters funnel. Beers are entered by
hand until the Buy Me a Coffee automation lands; everything downstream (the
event, best-effort attribution, the PostHog funnel + "beers this month" metric)
is the same the automated writer will feed.

Usage:
  python scripts/add_beer.py --email honza@example.cz              # 1 beer, 5 EUR
  python scripts/add_beer.py --email honza@example.cz --beers 3    # 3 beers, 15 EUR
  python scripts/add_beer.py --email x@y.cz --beers 2 --amount 12 --currency USD --at 2026-07-20

It (a) normalizes the email, (b) looks it up against the login accounts for
best-effort attribution (matched = it exists as an account_key under any
adapter), (c) inserts a row into the local `beers` audit table, (d) emits
`beer_purchased` to PostHog (distinct_id = the email, or `manual:anon:<id>` when
anonymous; event timestamp = the purchase date so it lands in the funnel when
the beer was bought), and (e) prints a one-line confirmation.

Egress rule (inherited from the telemetry design): identity + metadata only —
the email travels only as distinct_id; the supporter's name and any note are
never captured. Telemetry is fire-and-forget: a PostHog outage never blocks or
fails the DB write.

DB path resolves like daily_report.py: $DB_PATH, $DATA_DIR/gateway.db, /data,
./.localdata. Needs GATEWAY_SECRET in the env (to build config; the blob is
never decrypted here) and POSTHOG_API_KEY to actually emit — both present on
Railway, where the script runs:
  railway ssh --service gateway "python3 /app/scripts/add_beer.py --email honza@example.cz"
"""
from __future__ import annotations
import argparse
import math
import os
import sys
from datetime import datetime, timezone

# Make `garmin` importable when run from a checkout (src/ layout), not only
# when installed (Docker/uv).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from garmin import store, telemetry          # noqa: E402
from garmin.adapters import base             # noqa: E402
from garmin.config import load_config        # noqa: E402

# One beer = 5 EUR. A manual constant for hand entry; the future BMC writer will
# carry BMC's real per-donation amount instead.
BEER_PRICE_EUR = 5
DEFAULT_CURRENCY = "EUR"
_TS_FMT = "%Y-%m-%d %H:%M:%S"   # SQLite datetime('now') shape (UTC)


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def record_beer(conn, *, email: str | None, beers: int, amount: float | None,
                currency: str | None, created_at: datetime,
                source: str = "manual") -> dict:
    """Attribute → insert the `beers` row → emit `beer_purchased`. Returns a
    summary dict. `created_at` is the purchase time (a datetime): stored as a
    UTC string and passed through as the event timestamp. Anonymous when
    `email` is falsy (the future BMC writer's case)."""
    normalized = base.normalize_account_key(email) if email else None
    matched = bool(normalized) and store.account_key_exists(conn, normalized)
    row_id = store.add_beer(
        conn, email=normalized, beers=beers, amount=amount, currency=currency,
        matched=int(matched), source=source,
        created_at=created_at.strftime(_TS_FMT),
    )
    distinct_id = normalized if normalized else f"manual:anon:{row_id}"
    # Fire-and-forget: the row is already committed and capture swallows its own
    # errors, so a PostHog outage never touches the write. Identity/metadata
    # only — no name, no note.
    telemetry.capture(
        "beer_purchased", distinct_id=distinct_id,
        properties={"beers": beers, "amount": amount, "currency": currency,
                    "matched": matched, "source": source},
        timestamp=created_at,
    )
    return {"id": row_id, "email": normalized, "beers": beers, "amount": amount,
            "currency": currency, "matched": matched, "distinct_id": distinct_id}


def main():
    p = argparse.ArgumentParser(description="Record a beer donation + emit beer_purchased.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--email", required=True, help="supporter email (attribution key)")
    p.add_argument("--beers", type=int, default=1, help="beer count (default 1)")
    p.add_argument("--amount", type=float, default=None,
                   help=f"money value (default: beers x {BEER_PRICE_EUR})")
    p.add_argument("--currency", default=None,
                   help=f"currency (default {DEFAULT_CURRENCY})")
    p.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                   help="purchase date (default: today) — the event timestamp")
    args = p.parse_args()

    if args.beers < 1:
        p.error("--beers must be >= 1")
    if not args.email.strip():
        p.error("--email must not be empty")
    if args.amount is not None and (not math.isfinite(args.amount) or args.amount <= 0):
        p.error("--amount must be a finite number greater than zero")
    amount = args.amount if args.amount is not None else args.beers * BEER_PRICE_EUR
    currency = args.currency or DEFAULT_CURRENCY

    if args.at:
        try:
            created_at = datetime.strptime(args.at, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit(f"--at must be YYYY-MM-DD, got: {args.at!r}")
    else:
        created_at = datetime.now(timezone.utc)

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    telemetry.init(load_config(os.environ))    # no-op without POSTHOG_API_KEY
    conn = store.init_db(db_path)              # ensures the `beers` table exists
    try:
        result = record_beer(conn, email=args.email, beers=args.beers, amount=amount,
                             currency=currency, created_at=created_at)
    finally:
        conn.close()
        telemetry.shutdown()                   # flush the single event before exit

    tag = "matched" if result["matched"] else "unmatched"
    print(f"Recorded {result['beers']} beer(s) ({result['amount']} {result['currency']}) "
          f"for {result['email']} — {tag} (beers.id={result['id']}).")


if __name__ == "__main__":
    main()
