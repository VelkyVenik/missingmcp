# 01 — Oura API v2 facts

Type: research
Status: resolved

## Question

What does the Oura developer platform actually offer and constrain, as of July 2026?
Primary sources only (cloud.ouraring.com/docs, developer portal, official announcements).
Produce a markdown summary asset linked from this ticket. Specifically:

1. **Auth options**: OAuth2 app flow vs Personal Access Tokens — are PATs still issuable,
   any deprecation notices? Scopes available (and which cover sleep/readiness/activity/
   heart rate/workouts)?
2. **Token semantics** (OAuth path): access-token TTL, refresh-token behavior — does the
   refresh token rotate on use (WHOOP-style)? Any `offline`-scope analog required to get
   refresh tokens?
3. **Dev app registration**: does creating an app require an Oura account? A ring or
   membership? Is there an unapproved-app user cap (WHOOP has 10) and what does the
   approval process look like? Redirect-URI rules?
4. **Endpoints**: full usercollection endpoint list with pagination shape; which data is
   gated behind an active membership; is the v2 API read-only (any write endpoints at all)?
5. **Identity**: what does `personal_info` expose — is the account email available (needed
   for `account_key`, cf. map fog note)?
6. **Rate limits** and any sandbox/test-data facility.

## Answer

Resolved 2026-07-17. Full findings with citations: [assets/oura-api-v2-research.md](../assets/oura-api-v2-research.md).

1. **Auth**: OAuth2 authorization-code is the ONLY path — PATs were deprecated Dec 2025 and
   are no longer issuable. 8 scopes; `daily` covers sleep/readiness/activity summaries,
   `heartrate` time-series HR, `workout` workouts; plus `email`, `personal`, `tag`,
   `session`, spo2.
2. **Tokens**: refresh token is single-use and **rotates on every refresh (WHOOP-style)** —
   reuse the WhoopApi discipline (per-account lock + persist-before-use). No `offline`-scope
   analog; refresh tokens come by default. Access-token TTL: docs self-contradict (24h vs
   30d) — trust `expires_in` only.
3. **Registration**: needs an Oura account (no ring/membership documented as required).
   **Cap: 10 users until Oura approves the app**, unlimited after; approval channel
   api-support@ouraring.com, process undocumented. Redirect URIs exact-match, multiple
   allowed. New portal `developer.ouraring.com` mid-rollout beside cloud.ouraring.com.
4. **Endpoints**: 19 GET-only `/v2/usercollection/*` routes; pagination `start_date`/
   `end_date` + `next_token` (heartrate uses `start_datetime`/`end_datetime`). Fully
   read-only for data (only webhook management has writes). Gen3+ users **without active
   membership get no API data (403)**.
5. **Identity**: `personal_info` exposes email but it is **nullable and gated by the
   user-declinable `email` scope** — the connect flow must verify email presence (or key
   on the scope-free `id`) before minting an account_key.
6. **Limits/sandbox**: 5000 req / 5 min plus a newer two-layer regime with a
   **per-application aggregate bucket** (all gateway users share it) and `Retry-After`
   headers. Full sandbox at `/v2/sandbox/usercollection/*` — works without any account
   (verified live).

Watch-outs: scope string discrepancy `spo2` vs `spo2Daily` (verify empirically); blank
`scope` param requests all scopes and users can partially consent (granted scopes echoed
in redirect); membership lapse turns a connected user into 403s mid-life; OpenAPI
`servers` field is broken — real base is `api.ouraring.com`.
