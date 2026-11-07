# Activity means tool invocations; protocol traffic doesn't count

`tool_usage` records every JSON-RPC method under `tool`, because
`proxy._mcp_tool` returns the bare method for anything that isn't `tools/call`.
`active_accounts_between` counted all of it, so an MCP client that merely
completed its handshake registered as an active user. We now exclude a fixed set
of protocol methods (`store.PROTOCOL_METHODS`) from the activity metric.

## Why it mattered

Claude re-runs `initialize` + `tools/list` on every connector refresh, with no
user involved. Measured over four minutes of production traffic, 38 of 44
requests (86%) were protocol overhead and only 6 were real tool calls. For
2026-07-25 the daily Slack report claimed **83 active** accounts while PostHog
counted **48** accounts emitting `$mcp_tool_call` — the same day, the same users,
two definitions. The 35-account gap was people who had the connector installed
and used nothing.

## Why a denylist rather than a flag on the row

Marking each row as tool-vs-protocol at write time would be more robust, but it
needs a schema migration and would only classify rows written after it shipped.
A denylist reads correctly against the existing table, including history. The
cost is that a genuinely new protocol method is counted as activity until it is
added to the set — acceptable, because the MCP method list changes rarely and
visibly.

We keep *writing* protocol rows: they are the only record of which accounts have
a live connector, which `scripts/usage.py` and any future "connected installs"
metric still need. Only the activity read filters them.

## Consequences

- The reported active count drops by roughly a third the day this ships; the
  series is **not** comparable across that boundary. Nothing backfills — the
  history in `tool_usage` is unchanged, only its interpretation.
- A second, unfixed distortion remains in the same metric: `record_usage`
  overwrites `last_used` per `(adapter, account_key, tool)`, so an account that
  calls the same tool again the next day disappears from the earlier day's
  window. The count is therefore a lower bound, and fixing it needs a
  per-day record rather than an upserted row. (Fixed by [ADR 0003](0003-daily-activity-table.md).)
- PostHog measures the same thing from events and does not share either flaw; it
  is the more trustworthy of the two for activity.
