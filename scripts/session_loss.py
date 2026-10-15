"""Lost-MCP-session report: the share of garmin /mcp calls that fail on a
session the worker no longer has (404 "Session not found"), overall and right
after a worker start — the before/after measure for GARMIN_MCP_STATELESS_HTTP.

Reads `railway logs --json` output (JSON lines; other lines are skipped):

    railway logs --service gateway --json --lines 5000 \\
        --since 2026-10-14T18:30:00Z --until 2026-10-14T19:00:00Z \\
        --filter '@event:mcp-response OR @event:worker-started' >> before.jsonl
    python scripts/session_loss.py before.jsonl after.jsonl

Without `--lines` the CLI returns only 500 records, and it caps at 5000
(~40 min of traffic in 2026-10): fetch a long window in 30-min slices,
appending to one file, and redo a slice that returns exactly 5000 rows
with a shorter span. A row per file. 404 is only ever a lost session on this path (auth failures
are 401), and a request with a parsed tool/method is a POST — GET/DELETE carry
no body. Stateless workers answer neither 404 nor 400 for a missing/stale
session, so after the switch both should drop to ~0; the gateway answers
GET/DELETE itself with 405, which replaces the GET 409s and 15-min streams.
Does not expect fewer worker starts or fewer timeouts (504) — those are
separate problems.
"""
from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter, defaultdict
from datetime import datetime


def _ts(row: dict) -> float:
    # Railway timestamps carry nanoseconds; fromisoformat takes microseconds.
    raw = row["timestamp"].rstrip("Z")
    head, _, frac = raw.partition(".")
    return datetime.fromisoformat(f"{head}.{(frac or '0')[:6]}+00:00").timestamp()


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def report(rows: list[dict], after_start_s: float = 5.0) -> dict:
    starts: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("event") == "worker-started" and r.get("account"):
            starts[r["account"]].append(_ts(r))
    for v in starts.values():
        v.sort()

    def just_started(account: str, t: float) -> bool:
        s = starts.get(account, [])
        i = bisect.bisect_right(s, t) - 1
        return i >= 0 and t - s[i] <= after_start_s

    resp = [r for r in rows if r.get("event") == "mcp-response"
            and r.get("adapter", "garmin") == "garmin"]
    post = [r for r in resp if r.get("tool")]
    other = [r for r in resp if not r.get("tool")]
    lost = [r for r in post if r.get("status") == 404]
    early_post = [r for r in post if just_started(r.get("account", ""), _ts(r))]
    early_lost = [r for r in lost if just_started(r.get("account", ""), _ts(r))]
    times = sorted(_ts(r) for r in resp) or [0.0]

    def pct(n: int, d: int) -> str:
        return f"{100 * n / d:.2f}%" if d else "-"

    return {
        "window_h": round((times[-1] - times[0]) / 3600, 1),
        "worker_starts": sum(len(v) for v in starts.values()),
        "post": len(post),
        "404": len(lost),
        "404_share": pct(len(lost), len(post)),
        "404_accounts": len({r.get("account") for r in lost}),
        "400_share": pct(sum(r.get("status") == 400 for r in post), len(post)),
        f"post_within_{after_start_s:g}s_of_start": len(early_post),
        f"404_share_within_{after_start_s:g}s": pct(len(early_lost), len(early_post)),
        f"404s_within_{after_start_s:g}s": pct(len(early_lost), len(lost)),
        "get_delete_status": dict(Counter(r.get("status") for r in other)),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", help="railway logs --json outputs, one per window")
    ap.add_argument("--after-start-s", type=float, default=5.0,
                    help="'right after a worker start' window (default 5s)")
    args = ap.parse_args(argv)
    for path in args.files:
        print(path, json.dumps(report(load(path), args.after_start_s)))


if __name__ == "__main__":
    main()
