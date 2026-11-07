# Past-day activity comes from a per-day table, not tool_usage

`tool_usage` keeps one upserted row per `(adapter, account_key, tool)` with the
latest `last_used`. That answers "who has been active since X" exactly, but not
"who was active on day X": an account that calls the same tool again today
disappears from yesterday. ADR 0002 left this open as a known lower bound.

We now also write `daily_activity (adapter, account_key, day)`: one row per
account per **UTC** day on which it invoked a real tool (`store.is_activity`,
the same protocol-method denylist as ADR 0002). Both writers — `record_usage`
and `UsageBuffer` — fill it, the buffer taking the day at call time so a flush
across midnight can't move a call. `report.build_report` reads it, so the daily
report's "active" and the new 7-day active (WAU) are exact.

## Why

On 2026-11-06 the report said 2,022 active accounts while PostHog's
`mcp_active` said 2,663 for the same day. The operator tracks growth in WAU, and
the lower bound hid about a quarter of it.

## Decisions

- **UTC days, not Europe/Prague.** The table has to pick one bucket; UTC
  matches PostHog's `mcp_active`, so the two sources can be compared directly.
  The report switched to UTC days too (the header says so).
- **Retention 400 days** (`ACTIVITY_KEEP_DAYS`, pruned by the lifespan loop's
  data cleanup), so year-over-year stays possible. Roughly 2.5k rows/day.
- **The usage meter keeps reading `tool_usage`.** Its 30-day window ends now,
  where the latest `last_used` is exact.
- **No backfill.** History can't be reconstructed from `tool_usage`; the series
  starts on the deploy day, and the first report after the deploy covers a
  partial day.
- The table holds the account email (`account_key`), so every purge path
  (`purge_adapter`, `revoke.py --purge`) deletes from it too.
