# 03 — Auth model decision

Type: grilling
Status: resolved
Blocked by: 01

## Question

The auth *shape* is settled by facts (ticket 01): upstream OAuth, whoop pattern —
PATs are no longer issuable, and Oura's refresh tokens rotate single-use exactly like
WHOOP's, so the WhoopApi discipline (per-account lock, persist-before-use, no offline
scope needed) carries over. What remains to decide:

1. **account_key source**: `personal_info.email` is nullable AND gated by the
   user-declinable `email` scope. Options: (a) require the `email` scope and fail the
   connect with clear copy when it's declined/absent — keeps the CLAUDE.md invariant
   (account_key = normalized login email); (b) key on the scope-free Oura user `id` —
   works always, but is an explicit invariant deviation the spec must record.
2. **Scope request strategy**: request the explicit list we need vs. blank scope
   (= all). Users can partially consent; granted scopes are echoed in the redirect —
   decide which scopes are hard-required (connect fails without them) vs. degradable
   (tool returns a friendly "grant X to use this" error).
3. Confirm the `spo2` vs `spo2Daily` scope-string discrepancy handling (empirical check
   lands in implementation; the spec should note it).

## Answer

Resolved 2026-07-17 (grilling with the operator).

- **Auth shape** (settled by ticket 01's facts, confirmed as design): upstream OAuth,
  whoop pattern — `authorize_redirect_url`/`handle_callback` + gateway-owned refresh with
  the WhoopApi discipline (per-account asyncio.Lock, rotated blob persisted before use).
  No `offline`-scope analog needed; trust `expires_in` for TTL.
- **account_key**: normalized login email — the CLAUDE.md invariant holds, no exception.
  The `email` scope is HARD-REQUIRED: if the user declines it (or Oura returns a null
  email), the connect fails with clear copy ("we need your email to identify your
  account — grant the email permission and try again"). No keying on Oura user id, no
  hybrid fallback.
- **Scope strategy**: request an EXPLICIT list — every scope a tool will need (daily,
  heartrate, workout, session, tag, spo2, personal, email). Hard-required: `email`
  (identity) + `daily` (core value) — connect fails without them. All others are
  DEGRADABLE: the granted-scope set (echoed in the redirect) is persisted in the
  encrypted blob at connect time, and a tool whose scope is missing returns a friendly
  "grant X in Oura and reconnect" error instead of a raw 403.
- **Spec note**: the `spo2` vs `spo2Daily` scope-string discrepancy is verified
  empirically during implementation; the spec records both candidates.
