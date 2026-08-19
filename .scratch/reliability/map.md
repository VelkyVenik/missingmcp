# Wayfinder map: Fewer user-facing errors, quiet Slack

Label: wayfinder:map
Created: 2026-07-30

## Destination

Three things are true at the end of this map:

1. **Garmin token expiries are eliminated at the source** — the read-back fix
   (persist worker-rotated tokens) is deployed, and the backfill of the ~84
   affected accounts is decided and executed (execution is carried into this
   map for the already-decided fixes).
2. **The orphan-client sweep no longer deletes returning users** — the
   `last_seen`-based sweep (oauth-client-lifecycle §1) is deployed.
3. **Hourly "there are N errors" Slack pings are replaced by autonomous
   triage** — a designed (spec-ready) mechanism that analyzes problems and
   messages the operator only with a diagnosis and a proposed action. The
   mail-to-users decision is made on post-fix data (a "not needed" close is a
   valid outcome).

## Notes

- **Hybrid map** (Notes override of wayfinder's plan-only default): execution
  is carried for the already-decided fixes — tickets 02, 03, 04 and the
  backfill execution inside 05. Everything else (mail channel, triage
  mechanism) resolves to a decision + spec and is handed off.
- **Every code change goes via feature branch + PR** (CodeRabbit review),
  never straight to main; ask the operator before creating the branch/PR. See
  [[branch-pr-workflow]]. Outward-facing text shown first, see
  [[approve-public-posts]].
- **This repo is public**: no account e-mails or token values in any ticket,
  answer, or asset — aggregates only.
- **Prior context** (diagnosed before this map, in
  `.scratch/garmin-token-lifecycle/`):
  - [02 — refreshed tokens are discarded](../garmin-token-lifecycle/issues/02-persist-refreshed-garmin-tokens.md)
    (resolved research) — Garmin rotates the refresh token, `materialize()`
    overwrites the rotation with the stale DB blob on every spawn; 84/168
    accounts held a spent token on 2026-07-27. Proposed fix design lives there.
  - [01 — stale-token drop-off](../garmin-token-lifecycle/issues/01-stale-token-reconnect-dropoff.md)
    (ready-for-human) — 7-day measurement: 58 accounts hit, median recovery
    ~35 h, 20 never returned. The re-auth 401 works as a protocol, not as a
    notification.
- **Skills:** /grilling + /domain-modeling for grilling tickets; /tdd for the
  implementation tickets; /research for research tickets.
- **Operator's framing (charting grill, 2026-07-30):** "I want an autonomous
  mechanism that analyzes the problems and proposes what to do next — message
  me only when there's an action, not all day that errors exist."

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [What actually fills the hourly digest?](issues/01-digest-error-breakdown.md)
  — 433 problem rows/7d, zero 5xx; Garmin stale tokens are 41 % (74 accounts)
  and the correlated sign-in-failure classes push Garmin-expiry's true share
  toward ~70 %; baseline ~8.7 `<!here>` pings/day, and a post-fix simulation
  still leaves ~4/day (garminconnect 403 bursts, worker API failures,
  authorize scanner noise) — so ticket 08's triage signatures are needed, the
  fix alone won't quiet Slack. Asset:
  [assets/error-breakdown-7d.md](assets/error-breakdown-7d.md).
- [Implement the Garmin token read-back](issues/02-implement-garmin-token-readback.md)
  — shipped (PR #15, merge `0f7833b`): worker-rotated tokens are persisted
  back to the store at every capture point, and a respawn materializes the
  recovered rotation instead of the stale DB blob. Pre-fix drifted accounts
  (~84) are deliberately untouched — unblocks the
  [backfill decision](issues/05-backfill-decision.md).
- [Make the re-auth message carry the full instruction](issues/03-reauth-message-copy.md)
  — shipped (PR #16, merge `6779bc0`): the 401 body now spells out the
  recovery (sign in again when the client prompts; Claude: Settings →
  Connectors) and links the connector's landing page. Copy only, challenge
  shape and events unchanged.
- [Sweep orphan OAuth clients by last_seen](issues/04-orphan-sweep-last-seen.md)
  — shipped (PR #17, merge `6364359`): `last_seen` column (v2 migration),
  stamped by `get_client` on every authorize/token use; the sweep keys on
  activity, so returning users no longer hit "unknown client_id". Closes
  oauth-client-lifecycle §1.
- [Backfill: repair the accounts whose DB blob holds a spent token](issues/05-backfill-decision.md)
  — done (operator-approved, 2026-07-31): `scripts/backfill_garmin_tokens.py`
  persisted **117 drifted accounts**' latest rotation into the store (5 recent
  re-logins correctly skipped by the mtime-vs-updated_at guard; second run
  reads 0 drifted). Unblocks [post-fix verification](issues/06-post-fix-verification.md)
  (~a week of data).

- [Mid-stream failures on proxied worker responses](issues/09-midstream-asgi-failures.md)
  — diagnosed (2026-08-19): NOT a read-timeout — routine MCP session teardown;
  the worker aborts its open SSE stream (`httpx.RemoteProtocolError`) and the
  gateway logs a full ERROR traceback for it, twice per teardown, ~290
  rows/day, no demonstrable user impact. Fix graduated into
  [Quiet the stream-teardown noise](issues/10-quiet-stream-teardown.md).

## Not yet specified

- **Implementation of the autonomous triage mechanism** — once
  [08](issues/08-autonomous-triage-design.md) decides its shape (where it
  runs, what it reads, when it may message).
- **Known-signature → recommended-action catalogue (runbook)** feeding the
  triage mechanism — depends on ticket 01's breakdown and post-fix reality.
- **Mail infrastructure details** (provider, secrets, unsubscribe, copy) —
  only if ticket 07 decides mail is needed.
- **PostHog alerts/funnels realignment after the fix** (e.g. the
  "Worker start failures ≥3/hour" alert) — depends on ticket 06's numbers.
- **Whether the triage mechanism may autonomously file tickets into
  `.scratch/`** — an autonomy-boundary question for after 08.

## Out of scope

- **Auto-remediation** — the operator chose "the mechanism proposes, a human
  executes" (charting grill 2026-07-30); anything that automatically
  restarts/repairs production stays out of this effort.
- **New adapters** (oura, …), promotion, beer-metrics — separate efforts with
  their own maps.
