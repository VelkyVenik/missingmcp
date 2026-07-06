# WHOOP Adapter Design

**Date:** 2026-07-06
**Status:** approved design (brainstorming with the operator, both sections signed off)
**Branch:** feat/whoop-adapter (worktree; main checkout stays free for parallel work)

## Goal

A `whoop` adapter for MissingMCP with the same user-facing shape as garmin:
connect a personal WHOOP account to Claude via the `/whoop/mcp` connector
(OAuth 2.1 + DCR + PKCE toward Claude, path-scoped discovery), then ask about
recovery, sleep, strain, workouts and body measurements.

## Research summary (2026-07)

- WHOOP's only supported API is the **official v2 REST API** with OAuth 2.0
  authorization-code flow (confidential client). v1 sunset Oct 2025.
  Endpoints: cycles, recovery, sleep, workouts, profile, body measurements.
  Scopes are **read-only** (`read:profile read:body_measurement read:cycles
  read:recovery read:sleep read:workout`) plus `offline` for refresh tokens.
- Access token lives **1 hour**; refresh token is issued only with `offline`
  scope and **rotates on every use** (the old pair is invalidated; WHOOP
  recommends serializing refreshes).
- App registration is instant self-service at developer-dashboard.whoop.com
  (max 5 apps/account). An unapproved app is limited to **10 WHOOP members**
  — fits the trusted-circle model. Rate limit: 100 req/min and 10k req/day
  **per app** (not per member).
- WHOOP ships **no official MCP server** (unlike Rohlík), so the adapter is
  not redundant. No community MCP server is a drop-in `garmin_mcp` equivalent
  (headless + official API + tokens via env + HTTP transport does not exist).
  Best Python building block found: `whoopy` (official v2, token reuse) — but
  the surface we need is small enough that plain httpx suffices.
- An unofficial password-grant/iOS API exists (richer, has writes) but is
  ToS-questionable and fragile. **Decision: rejected.** Read-only official
  API only ("read-only stačí" — operator, 2026-07-06).

## Decisions

1. **Official OAuth v2 API, read-only.** No unofficial API, no password
   handling — the gateway never sees WHOOP credentials at all.
2. **Approach A — in-tree, in-process WHOOP MCP** (chosen over reusing a
   community MCP server as a worker). Rationale: the rotating refresh token
   makes any self-refreshing worker unsafe — after an idle-reap respawn from
   the stale DB blob, the consumed refresh token would force re-auth. The
   gateway must own refresh and persist rotations immediately, which an
   in-process implementation gets for free. Bonus: no Node runtime, no
   ports, no reaper involvement.
3. **Hand-rolled MCP protocol layer** (no `mcp` SDK dependency): a stateless
   tools-only server needs just `initialize`, `notifications/initialized`,
   `tools/list`, `tools/call`, `ping` — ~100 lines of JSON-RPC, in the
   spirit of the dependency-free SigV4 signer in `backup.py`. Responses are
   `application/json` (allowed by the MCP spec), no session id.

## Architecture

### Login: third seam shape — upstream OAuth redirect

The existing seam supports form login (+ optional second factor). WHOOP is a
proper OAuth provider, so the adapter contract in `adapters/base.py` gains a
duck-typed variant (dispatch via `is_upstream_oauth(adapter)`, mirroring
`is_remote`):

- `authorize_redirect_url(state_id: str) -> str` — builds the WHOOP authorize
  URL (`client_id`, `redirect_uri = PUBLIC_URL/whoop/oauth/callback`,
  `scope`, `state=state_id`). WHOOP requires `state` ≥ 8 chars; our ids are
  `security.new_secret(18)`.
- `handle_callback(query: Mapping[str, str]) -> LoginOk` — exchanges the code
  (confidential client: `WHOOP_CLIENT_SECRET`), fetches
  `/v2/user/profile/basic`, derives `account_key =
  normalize_account_key(email)` (the existing invariant holds: account_key is
  the normalized login email), builds the blob. Raises `LoginError`.

Flow in `oauth.py`:

- `authorize_get` branches: upstream-OAuth adapter → validate `client_id` /
  `redirect_uri` / PKCE exactly as today → stash Claude's OAuth params in
  `AuthState` (same mechanics as the MFA pending map: TTL 300 s, one-time
  pop, adapter-scoped) under a fresh `state_id` → 302 to WHOOP. Form
  adapters keep today's behavior.
- New handler `oauth.authorize_callback` (route
  `/{adapter}/oauth/callback`, GET, registered only for upstream-OAuth
  adapters): pop the stash by `state` (one-time pop doubles as CSRF
  protection), re-validate client + redirect_uri against the DCR record,
  call `adapter.handle_callback`, then `adapter.verify(blob)`, then the
  existing `_finish` (upsert + code mint + redirect back to Claude).
  **Verify-then-persist is unchanged: `_finish` stays gated on
  `adapter.verify` on this path too.** For whoop, `verify` re-fetches
  `/v2/user/profile/basic` with the blob's access token and returns the
  display name — a cheap second call that keeps the invariant uniform
  across adapters.
- Callback errors (user denied at WHOOP, `state` expired/unknown, code
  exchange failed, profile fetch failed): there is no form to re-render, so
  render a small error page fragment (shared site layout) — "WHOOP
  connection failed — go back to Claude and try again", HTTP 400. Denial is
  not an anomaly; log as a warn-level structured event, not an error.

### Blob

JSON, AES-256-GCM-encrypted at rest like every account blob:

```json
{"access_token": "...", "refresh_token": "...", "expires_at": 1751800000,
 "user_id": 12345, "email": "user@example.com"}
```

### Token refresh — gateway-owned, serialized, persisted immediately

- Every WHOOP API call goes through the adapter's client (`api.py`), which
  checks `expires_at` with a ~120 s margin. Stale → refresh under a
  **per-account `asyncio.Lock`** (process-local dict; the gateway is
  single-node by design) → POST the token endpoint with
  `grant_type=refresh_token` → **persist the rotated blob to SQLite
  immediately** (before the API call proceeds). Double-check inside the lock
  so queued waiters reuse the fresh token instead of refreshing again.
- A 401 from a data endpoint despite a seemingly-fresh token triggers one
  forced refresh + retry, then fails.
- Refresh failure with `invalid_grant` (rotation lost, token revoked) → the
  account needs re-auth → surface as **502 `whoop_session_expired`** (same
  shape and copy pattern as the other adapters, so Claude prompts a
  reconnect).

### Forward: third strategy — `LocalForward`

```python
class LocalForward(Protocol):
    async def handle(self, conn, account_key: str, body: bytes) -> tuple[int, dict, bytes]:
        """Handle an MCP JSON-RPC request in-process; returns (status, headers,
        body). Receives conn + account_key because a token refresh must
        persist the rotated blob."""
```

- Dispatch stays duck-typed (`is_local` by the presence of `handle`), beside
  `is_remote`. `proxy.handle_mcp` keeps its shared core (Bearer auth +
  rate limits, body limit, usage metrics, `mcp-request`/`mcp-response` log
  events with `ttfb_ms`/`total_ms`/`bytes`) and gains a local branch that
  calls `forward.handle(...)` instead of streaming to an upstream.
- `GET`/`DELETE /whoop/mcp` → **405** (stateless server, no server-initiated
  streams, no sessions — explicitly allowed by the MCP spec).
- `app.py` creates **no WorkerManager** for local-forward adapters (same as
  remote).

### MCP protocol layer (`adapters/whoop/mcp.py`)

Stateless JSON-RPC over streamable HTTP, single (non-batch) requests:

- `initialize` → negotiated protocol version, capabilities `{"tools": {}}`,
  serverInfo. No `Mcp-Session-Id` issued.
- `notifications/initialized` (and other notifications) → 202, empty body.
- `tools/list` → static tool table.
- `tools/call` → dispatch to the tool function; tool-level upstream failures
  return a JSON-RPC **result** with `isError: true` (MCP tool-error shape),
  not a protocol error.
- `ping` → empty result. Unknown method → JSON-RPC error −32601.

### Tools (read-only, WHOOP v2)

| Tool | Endpoint | Params |
|---|---|---|
| `get_profile` | `/v2/user/profile/basic` | — |
| `get_body_measurements` | `/v2/user/measurement/body` | — |
| `get_cycles` | `/v2/cycle` | `start`, `end`, `limit`, `next_token` |
| `get_recoveries` | `/v2/recovery` | dtto |
| `get_sleeps` | `/v2/activity/sleep` | dtto |
| `get_sleep` | `/v2/activity/sleep/{id}` | `id` (UUID) |
| `get_workouts` | `/v2/activity/workout` | dtto collections |
| `get_workout` | `/v2/activity/workout/{id}` | `id` (UUID) |

Collections take ISO-date `start`/`end`, pass WHOOP's `nextToken` pagination
through (`next_token` in/out); Claude fetches further pages itself. No
aggregation tools (trends are Claude's job — YAGNI). Exact v2 paths (the
`/developer` prefix) get verified against developer.whoop.com/api during
planning; base URLs are config so tests can point at a fake upstream.

Upstream 429 (app-wide 100 req/min) → tool error "WHOOP rate limit hit, try
again in a minute" — no gateway-side throttling for a trusted circle.

### Configuration

- `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET` — from the operator's app at
  developer-dashboard.whoop.com (redirect URI
  `https://missingmcp.com/whoop/oauth/callback`, all six read scopes +
  `offline`). **The adapter registers only when both are set** (like
  `BACKUP_S3_*`): local dev and CI run without WHOOP credentials, production
  enables it via env.
- `WHOOP_API_BASE` (default `https://api.prod.whoop.com`) — override for
  tests/staging; both the OAuth endpoints (`/oauth/oauth2/auth`,
  `/oauth/oauth2/token`) and the data API derive from it.

### Files

```
src/missingmcp/adapters/whoop/
  __init__.py   # WhoopAdapter (authorize_redirect_url, handle_callback,
                # verify, landing_template) + WhoopLocalForward wiring
  api.py        # httpx client: code exchange, refresh-with-lock-and-persist,
                # GET helpers with one forced-refresh retry on 401
  mcp.py        # JSON-RPC handler + tool table (name, description,
                # inputSchema, fn)
src/missingmcp/templates/whoop.html   # connector landing fragment (_layout)
scripts/gen_whoop_tools.py            # regenerates the All-tools block from
                                      # the in-tree tool table (see below)
```

Plus: seam additions in `adapters/base.py`, authorize/callback branches in
`oauth.py`, local branch in `proxy.py`, conditional registration in
`adapters/__init__.py` + callback route in `app.py`, WHOOP card on
`home.html`, README (env vars, app-registration walkthrough, member-limit
note), CLAUDE.md.

### Landing page — same skeleton as the garmin connector page

`whoop.html` follows the connector-page template established by `garmin.html`
(its header comment mandates the section order for new connectors): hero
(pill, headline, CTA + server URL `{PUBLIC_URL}/whoop/mcp`) → "What is this?"
→ "What Claude can see" cards (recovery, sleep, strain/cycles, workouts, body
measurements) → "How to connect" 4 steps → "Tips & tricks" prompt cards →
"Under the hood" → "All tools" → final CTA. WHOOP-specific copy differences:

- Connect step 3: no credentials entered on our site — "you're redirected to
  WHOOP, sign in there and approve read access". The security note leads
  with this trust advantage (the gateway never sees the WHOOP password;
  only encrypted OAuth tokens are stored).
- "Under the hood": no upstream open-source worker to credit — the WHOOP
  connector is built into missingmcp itself on WHOOP's official developer
  API (link developer.whoop.com), gateway repo link as on the garmin page.
- "All tools": generated from the in-tree tool table by
  `scripts/gen_whoop_tools.py` (pattern: `gen_garmin_tools.py`), rewriting
  the `GENERATED:TOOLS` block so page and code never drift.

## Security invariants (unchanged plus new ones)

- WHOOP tokens live **only** in the encrypted blob; never logged (log at most
  expiry timestamps and the 8-char account-key hash prefix), never
  materialized to files (no worker → no `materialize`).
- `WHOOP_CLIENT_SECRET` stays in env/config; never in the DB, never in logs.
- The `state` stash is one-time-pop with 300 s TTL and adapter scoping;
  callback re-validates the DCR client + redirect_uri allowlist before
  `_finish`.
- PKCE S256 toward Claude is untouched. The upstream leg is a confidential-
  client code flow per WHOOP's docs (no PKCE requirement published); if the
  dashboard later offers PKCE for the upstream leg too, adding it is a
  planning-stage detail, not a design change.
- Log events remain a stable schema; new events: `whoop-refresh-ok`,
  `whoop-refresh-failed`, `upstream-oauth-start`, `upstream-oauth-callback`
  (status: ok/denied/error).

## Testing

- No real WHOOP anywhere in the suite. A **fake WHOOP upstream** in
  `tests/conftest.py` (pattern: `fake_worker`/`fake_remote`) serves
  `/oauth/oauth2/token`, profile and data endpoints; `WHOOP_API_BASE` points
  at it.
- Coverage: authorize redirect (stash + exact WHOOP URL params), callback
  happy path end-to-end (exchange → profile → persist → code mint → redirect
  to Claude), callback denied / expired state / unknown state, refresh with
  rotation (**assert the rotated refresh token was persisted**), concurrent
  tool calls refresh exactly once (lock), refresh `invalid_grant` → 502
  `whoop_session_expired`, MCP handshake + `tools/list` + `tools/call`
  against the fake upstream, 405 on GET/DELETE, tool errors on upstream
  5xx/429, usage metrics rows, path-scoped discovery documents for `whoop`.
- **Release gate:** manual e2e with the operator's real WHOOP account —
  register the real dev app, run the full Claude connect flow (discovery →
  DCR → authorize → WHOOP consent → token → tool calls), verify a refresh
  survives an hour-plus session. Mirrors the garmin release gate.

## Out of scope

- Writes (journal, alarms) — only exist on the unofficial API; rejected.
- WHOOP webhooks (no push use-case; the gateway pulls on demand).
- App approval for >10 members (submit only if the circle outgrows 10).
- Worker/remote strategies for whoop; a future adapter can still choose them.
