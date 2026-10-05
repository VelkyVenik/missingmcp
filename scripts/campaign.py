#!/usr/bin/env python3
"""Operator CLI for mail campaigns (sending itself is done by the gateway's
lifespan loop — see src/missingmcp/mailer.py).

Usage:
  python scripts/campaign.py create <slug> [--audience all|users|subscribers]
        # snapshot campaigns/<slug>.txt and enqueue every not-unsubscribed
        # recipient, most active users first (status: draft)
  python scripts/campaign.py test <slug> --to me@example.com
        # send the rendered mail once to one address (not recorded in the ledger)
  python scripts/campaign.py start <slug>      # draft|paused → active: the gateway starts sending
  python scripts/campaign.py pause <slug>      # stop after the current batch
  python scripts/campaign.py status [<slug>]   # counts, sent in last 24h, ETA (no addresses)
  python scripts/campaign.py status <slug> --unknown   # + list the `unknown` addresses
  python scripts/campaign.py requeue-unknown <slug>    # send the `unknown` ones again
  python scripts/campaign.py requeue-failed <slug>     # retry the `failed` ones (attempts reset)
  python scripts/campaign.py unsubscribe <email>       # manual opt-out (e.g. a reply)

campaigns/<slug>.txt is plain text: a first line `Subject: ...`, a blank line,
then the body. The unsubscribe footer is appended automatically.

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
`test` also needs the MAIL_* env (present on the gateway service).
On Railway: railway ssh --service gateway "python3 /app/scripts/campaign.py status"
"""
from __future__ import annotations
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from missingmcp import config, mailer, security, store  # noqa: E402

CAMPAIGNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "campaigns")


def _default_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def load_text(slug: str, directory: str = CAMPAIGNS_DIR) -> tuple[str, str]:
    """(subject, body) from campaigns/<slug>.txt."""
    with open(os.path.join(directory, f"{slug}.txt"), encoding="utf-8") as f:
        first, _, rest = f.read().partition("\n")
    if not first.startswith("Subject:"):
        sys.exit(f"campaigns/{slug}.txt must start with a 'Subject: ...' line")
    subject, body = first[len("Subject:"):].strip(), rest.strip("\n")
    if not subject or not body.strip():
        sys.exit(f"campaigns/{slug}.txt: empty subject or body")
    return subject, body


def _campaign(conn, slug):
    c = store.get_campaign(conn, slug)
    if c is None:
        sys.exit(f"no campaign {slug!r}")
    return c


def _cap() -> int:
    return config.mail_daily_cap(os.environ)


def cmd_create(conn, args):
    subject, body = load_text(args.slug, args.dir)
    if store.get_campaign(conn, args.slug):
        sys.exit(f"campaign {args.slug!r} already exists")
    people = store.campaign_audience(conn, args.audience)
    valid = [e for e in people if security.valid_email(e)]
    store.create_campaign(conn, args.slug, subject, body, args.audience, valid)
    print(f"created {args.slug!r} (draft): {len(valid)} recipients"
          + (f", {len(people) - len(valid)} skipped as malformed" if len(valid) < len(people) else ""))
    print(f"subject: {subject}")
    print(f"next: test it (campaign.py test {args.slug} --to <you>), then start it")


def cmd_test(conn, args):
    cfg = config.load_config()
    c = _campaign(conn, args.slug)
    url = mailer.unsubscribe_url(cfg.public_url, "test-preview")
    res = mailer.send(cfg, args.to, c["subject"], mailer.render_text(c["body"], url),
                      mailer.list_unsubscribe_headers(url, cfg.mail_reply_to))
    print(f"test sent: {mailer.classify(res, args.to)} (message_id {res.get('message_id')})")


def cmd_start(conn, args):
    c = _campaign(conn, args.slug)
    if c["status"] not in ("draft", "paused"):
        sys.exit(f"campaign is {c['status']}; only draft/paused can start")
    store.set_campaign_status(conn, c["id"], "active")
    print(f"{args.slug}: active — the gateway sends up to {_cap()}/day from its next tick")


def cmd_pause(conn, args):
    c = _campaign(conn, args.slug)
    if c["status"] != "active":
        sys.exit(f"campaign is {c['status']}, not active")
    store.set_campaign_status(conn, c["id"], "paused")
    print(f"{args.slug}: paused")


def cmd_status(conn, args):
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"sent in last 24h (all campaigns): {store.sent_since(conn, since)} / {_cap()}")
    camps = [_campaign(conn, args.slug)] if args.slug else store.list_campaigns(conn)
    for c in camps:
        n = store.campaign_counts(conn, c["id"])
        line = " · ".join(f"{k} {v}" for k, v in n.items() if v)
        eta = f" · ~{math.ceil(n['pending'] / _cap())} more day(s)" if n["pending"] else ""
        print(f"{c['slug']} [{c['status']}, {c['audience']}] {line or 'empty'}{eta}")
        if args.unknown:
            for email in store.unknown_sends(conn, c["id"]):
                print(f"  unknown: {email}")


def _requeue(conn, slug, status):
    c = _campaign(conn, slug)
    n = store.requeue(conn, c["id"], status)
    print(f"{slug}: {n} {status} → pending")
    if n and c["status"] == "done":
        # the mailer only drains active campaigns — reopen it for the operator
        store.set_campaign_status(conn, c["id"], "paused")
        print(f"{slug}: was done, now paused — run `start {slug}` to send them")
    elif n and c["status"] != "active":
        print(f"{slug}: is {c['status']} — run `start {slug}` to send them")


def cmd_requeue_unknown(conn, args):
    _requeue(conn, args.slug, "unknown")


def cmd_requeue_failed(conn, args):
    _requeue(conn, args.slug, "failed")


def cmd_unsubscribe(conn, args):
    store.add_unsubscribe(conn, args.email.strip().lower(), "manual")
    print("unsubscribed (pending sends to the address dropped)")


def main(argv=None):
    p = argparse.ArgumentParser(description="Mail campaigns (operator CLI).")
    p.add_argument("--db", default=None, help="SQLite DB path")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("create")
    s.add_argument("slug")
    s.add_argument("--audience", choices=("all", "users", "subscribers"), default="all")
    s.add_argument("--dir", default=CAMPAIGNS_DIR, help=argparse.SUPPRESS)
    s = sub.add_parser("test")
    s.add_argument("slug")
    s.add_argument("--to", required=True)
    for name in ("start", "pause", "requeue-unknown", "requeue-failed"):
        sub.add_parser(name).add_argument("slug")
    s = sub.add_parser("status")
    s.add_argument("slug", nargs="?")
    s.add_argument("--unknown", action="store_true")
    sub.add_parser("unsubscribe").add_argument("email")
    args = p.parse_args(argv)

    db = args.db or _default_db()
    if not os.path.exists(db):
        sys.exit(f"DB not found: {db}\nSet --db, DB_PATH or DATA_DIR.")
    conn = store.init_db(db)   # creates the campaign tables on an older DB
    try:
        {"create": cmd_create, "test": cmd_test, "start": cmd_start, "pause": cmd_pause,
         "status": cmd_status, "requeue-unknown": cmd_requeue_unknown,
         "requeue-failed": cmd_requeue_failed,
         "unsubscribe": cmd_unsubscribe}[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
