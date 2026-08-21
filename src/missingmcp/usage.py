"""Public usage meter: the per-connector "N people used this in the last 30
days" line rendered into the landing pages as social proof.

Aggregate only — a single count, never per-user activity (the pages must not
look like we track what individuals do). Shown only from MIN_COUNT up: a
"3 people" meter reads as a ghost town and hurts more than no meter at all,
so below the threshold the snippet is empty and the page renders exactly as
before. The count is a process-local TTL cache over
`store.active_accounts_between` (the same single-node assumption as the rest
of the gateway's in-memory state), so serving a landing page stays one string
substitution per request instead of a DB query.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import store

WINDOW_DAYS = 30   # "active" = invoked a real tool inside this rolling window
MIN_COUNT = 10     # below this, render nothing
CACHE_TTL = 600    # seconds between DB refreshes; the count moves slowly


def _utc(dt: datetime) -> str:
    # The store's datetime('now') format — lexicographic == chronological.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class UsageMeter:
    def __init__(self, conn):
        self._conn = conn
        self._counts: dict[str, int] = {}
        self._expires = 0.0

    def count(self, adapter: str) -> int:
        now = time.monotonic()
        if now >= self._expires:
            utc = datetime.now(timezone.utc)
            # End bound a day ahead: last_used can't exceed "now", the margin
            # just keeps the [start, end) window safely closed over it.
            self._counts = store.active_accounts_between(
                self._conn,
                _utc(utc - timedelta(days=WINDOW_DAYS)),
                _utc(utc + timedelta(days=1)),
            )
            self._expires = now + CACHE_TTL
        return self._counts.get(adapter, 0)

    def snippet(self, adapter: str) -> str:
        """Trusted HTML for a {USAGE_METER_<ADAPTER>} placeholder, or "" below
        the display threshold. MIN_COUNT >= 2, so the copy is always plural."""
        n = self.count(adapter)
        if n < MIN_COUNT:
            return ""
        return (f'<span class="usage-meter">{n} people used this in the '
                f'last 30 days</span>')
