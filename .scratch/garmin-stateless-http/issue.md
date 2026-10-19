# Stateless Streamable HTTP for Garmin workers

Type: task
Status: resolved (2026-10-17 — PR #49 deployed 2026-10-15 19:46 UTC, measured)

## Why

MCP sessions lived inside each worker process. At `MAX_WORKERS=60` (idle TTL
900 s) production replaced ~13,200 workers/day, ~13,000 of them through cap
eviction (median idle before eviction 207 s). The client's next call carried
the old `Mcp-Session-Id` and the new worker answered 404 "Session not found":
3,940 such 404s across 930 accounts in 24 h (2026-10-14 18:30 → 10-15 18:30
UTC), 85 % within 5 s of a worker start. Some clients re-initialize, some
surface a failed call. garmin_mcp `e79df6f` already supported
`GARMIN_MCP_STATELESS_HTTP=true`; production had it unset.

## What shipped (PR #49)

- `GARMIN_MCP_STATELESS_HTTP` (config `garmin_stateless_http`), **default on**,
  validated strictly at gateway start (garmin_mcp exits on an unknown value,
  which would fail every spawn); `GarminWorkerForward.env` always passes it
  explicitly. `false` is the rollback switch (a Railway variable → redeploy).
- With it on, `proxy.handle_mcp` answers GET/DELETE with 405 (`Allow: POST`)
  before touching a worker: a stateless GET stream can never carry a message
  but held the worker in flight until Railway's 15-min edge cutoff, and in
  stateless mode every GET (no more 409) would have done so.
- Regression test `tests/test_stateless_worker.py`: real proxy +
  WorkerManager + a stub worker on the real MCP SDK (1.30.0, dev extra);
  stateful run kept as the control that reproduces the 404.
- `scripts/session_loss.py`: the before/after measure from `railway logs`.

Out of scope, unchanged: `MAX_WORKERS`, TTL, timeouts, blocking tool
execution, interrupted streams reported as 200, retries of write tools.

## Result (24 h before vs 24 h after, `scripts/session_loss.py`)

| | before (10-14 19:30 → 10-15 19:30) | after (10-16 00:00 → 10-17 00:00) |
|---|---|---|
| POST calls | 132,973 | 126,507 |
| 404 Session not found | 3,514 (2.64 %, 897 accounts) | **0** |
| 400 Missing session ID | 2,676 (2.01 %) | **0** |
| GET/DELETE | 11,275 (663 × 409) | 352, all 405 |
| worker starts / evictions | 13,230 / 13,001 | 14,989 / 14,841 |
| 200 latency p50 / p99 | 58 ms / 23.4 s | 48 ms / 23.7 s |

Clients stopped opening GET listen streams after the 405. Timeouts and
interrupted streams rose (50 → 134, 110 → 194) but one account drove 82 and 83
of them in a single hour; without the top two accounts 22 → 28 and 70 → 87 —
the known slow-Garmin-query class, not this change. Starts/evictions +13 %,
consistent with workers no longer pinned by 15-min GET streams.

## Not verified

Real Claude / ChatGPT clients were not driven against a test gateway; the
compatibility evidence is protocol tests plus unchanged production traffic
(2,707 vs 2,761 active accounts, only 200/202 on POST) and the WHOOP local
strategy, which has been stateless with 405 on GET all along.

Rollback note: after a stateless period clients hold no session id, so a
switch back to stateful answers their next call 400 "Missing session ID"
until they re-initialize.
