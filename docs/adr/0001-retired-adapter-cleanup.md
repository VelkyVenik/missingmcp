# Retired-adapter data is identified by an explicit list, not registry-absence, and fully purged

The periodic cleanup loop removes data for retired adapters (e.g. `rohlik`,
retired 2026-07-06). We identify a retired adapter by an explicit hardcoded
`RETIRED_ADAPTERS` set — **not** by "absent from the live registry
(`adapters.build_adapters`)" — and for any adapter on that list we delete *all*
rows across every table (`accounts`, `access_tokens`, `oauth_clients`,
`oauth_codes`, `tool_usage`).

## Why not registry-absence

The registry is config-dependent: the whoop adapter only registers when both
`WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` are set. If "retired" meant "not in
the registry", a single missing/typo'd env var on deploy would make whoop look
retired and **permanently delete every whoop user's stored credentials and
tokens**. An explicit list means only what we deliberately name as dead gets
touched; a config mistake can never trigger data loss. The cost is that retiring
an adapter is now a two-step code change (remove from `build_adapters` *and* add
to `RETIRED_ADAPTERS`) — acceptable, since retiring is already a deploy.

## Why a full purge

A retired adapter has no `/<adapter>/mcp` route, so its tokens and accounts are
unreachable and serve no purpose — keeping encrypted upstream credentials for a
service we no longer offer is a liability, not an asset (cf. the WHOOP ToU
obligation to delete stored content on termination). Off-box S3 backups
(weekday-rotated) make an erroneous purge recoverable.

## Consequences

- Orphan-client cleanup (0-token clients older than 1h) already sweeps the
  *client* rows of a dead adapter, so `RETIRED_ADAPTERS` earns its keep only for
  the case orphan-cleanup can't reach: a user who still held a **live token** when
  the adapter was pulled.
- The purge is destructive and runs unattended in the lifespan loop. It is gated
  entirely by the explicit list; an empty list is a no-op.
