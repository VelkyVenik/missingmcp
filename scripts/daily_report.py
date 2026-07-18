#!/usr/bin/env python3
"""Daily user-stats report for the MCP gateway (reads the DB read-only).

Yesterday's new / active / total users per connector, plus a 7-day new count —
the exact same numbers the in-app daily Slack report posts, because this shares
`missingmcp.report.build_report` / `render_slack`. Prints by default; --post
actually sends to Slack.

Usage:
  python scripts/daily_report.py                       # print yesterday's report
  python scripts/daily_report.py --post                # also POST to Slack
  python scripts/daily_report.py --now 2026-07-18      # report the day BEFORE this date
  python scripts/daily_report.py --tz UTC              # override the timezone

DB path resolves like usage.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
Webhook: --webhook or $SLACK_WEBHOOK_URL (required only with --post).
On Railway: railway ssh --service gateway "python3 /app/scripts/daily_report.py"
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Make `missingmcp` importable when run from a checkout (src/ layout), not only
# when installed (Docker/uv).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from missingmcp import report as report_mod  # noqa: E402


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
    p = argparse.ArgumentParser(description="MCP gateway daily user-stats report.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--post", action="store_true", help="POST to Slack (else just print)")
    p.add_argument("--now", default=None,
                   help="ISO datetime/date; the report covers the day BEFORE it "
                        "(default: real now)")
    p.add_argument("--tz", default="Europe/Prague", help="timezone (default Europe/Prague)")
    p.add_argument("--webhook", default=None,
                   help="Slack webhook URL (default: $SLACK_WEBHOOK_URL)")
    args = p.parse_args()

    tz = ZoneInfo(args.tz)
    if args.now:
        now_local = datetime.fromisoformat(args.now)
        now_local = now_local.replace(tzinfo=tz) if now_local.tzinfo is None \
            else now_local.astimezone(tz)
    else:
        now_local = datetime.now(tz)

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    conn = report_mod.open_ro(db_path)
    try:
        rep = report_mod.build_report(conn, now_local)
    finally:
        conn.close()
    text = report_mod.render_slack(rep)
    print(text)

    if args.post:
        webhook = args.webhook or os.environ.get("SLACK_WEBHOOK_URL", "")
        if not webhook:
            sys.exit("--post needs a webhook: pass --webhook or set SLACK_WEBHOOK_URL.")
        report_mod.post_slack(webhook, text)
        print("\n[posted to Slack]")


if __name__ == "__main__":
    main()
