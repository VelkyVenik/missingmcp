# Garmin MCP Gateway — Design

**Date:** 2026-06-30
**Status:** Approved design (pre-implementation)

## Problem

`garmin_mcp` (`github.com/Taxuspt/garmin_mcp`) is a single-user, stdio MCP server.
It authenticates to Garmin Connect with one account (env credentials + cached
OAuth tokens) and exposes ~40 tools. We want to:

1. Use it from the **Claude mobile app** (and web/desktop), which only speaks to
   **remote MCP servers over HTTPS with OAuth**.
2. Let **multiple people** (a small, trusted circle — me + family/friends) each
   connect **their own** Garmin account.
3. Do this **without modifying `garmin_mcp`** — wrap a gateway *around* it and
   consume it as a black-box dependency. Nothing is merged into that project.

The reference shape is `rohlik-oauth-proxy`: an OAuth 2.1 server that fronts an
upstream MCP server. The key difference: Rohlik *hosts* its own MCP server and
only needs credential headers injected, so its proxy is a thin reverse-proxy.
Garmin has **no hosted MCP server** — `garmin_mcp` is a process that logs in to
Garmin statefully (often with **MFA**) and caches tokens. So the gateway must
*provision* per-user Garmin sessions, not just forward headers.

## Decisions (from brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Audience | Small trusted circle (2–10 known people) | Same trust model as rohlik; storing encrypted per-user secrets on own infra is acceptable. |
| Garmin auth | **Web login → store tokens only** | Best UX (type creds on OAuth page). Server logs in, keeps only the resulting tokens, **discards the password**. Re-auth ~yearly. |
| Stack | **All-Python** (Starlette/FastAPI) | Garmin login + tools need Python; one runtime, no IPC. |
| Multi-user isolation | **Per-user `garmin-mcp` subprocess** (variant B) | Keeps `garmin_mcp` 100% unmodified — uses only its documented CLI + env contract. |
| Hosting | **Docker Compose on own VPS, nginx (TLS + domain) in front** | Same setup as rohlik. |
| `garmin_mcp` integration | Black-box dependency, pinned to a git commit | No source changes, no in-process imports of its internals; supply-chain pinning. |

### Why not the alternatives

- **Variant A (in-process contextvar proxy):** would import `garmin_mcp`'s
  internal `configure()`/`register_tools()` and run in-process. Even without
  editing files, it couples to internal API and reaches into the project.
  Rejected per the "gateway around, black box" constraint.
- **Variant C (stateless client per request):** `login()` does a token-validation
  roundtrip + refresh every request → latency and Garmin rate-limit risk.
- **Storing email+password:** for MFA accounts the password can't silently
  re-auth anyway, so it adds the largest secret to the breach blast radius for
  little benefit.

## Goals / Non-goals

**Goals**
- Remote, OAuth-protected MCP endpoint usable from Claude mobile/desktop/web.
- Per-user Garmin accounts; phone + desktop of the same user share one worker.
- Garmin password never persisted; tokens encrypted at rest.
- Persistent record of who has authenticated (survives restarts).
- `garmin_mcp` runs unmodified.

**Non-goals**
- Public open signup / abuse-hardening for untrusted users.
- Reimplementing Garmin's API or the 40 tools.
- A management UI beyond a basic landing page (admin is CLI/DB-level for now).

## Architecture

```
 Claude (phone / desktop / web)
        │  POST /mcp  (Authorization: Bearer <token>)   over HTTPS
        ▼
   [ nginx ]  ── TLS termination + public domain (operator-managed)
        │  http (localhost)
        ▼
┌──────────────────────── GATEWAY (this project, Python/Starlette) ───────────────────────┐
│                                                                                          │
│  OAuth 2.1 layer (mirrors rohlik)        SQLite + AES-256-GCM store (on /data volume)     │
│   /.well-known/...   /oauth/register      · oauth_clients                                 │
│   /oauth/authorize   /oauth/token         · oauth_codes (PKCE, one-time, 10 min)          │
│                                           · garmin_accounts: enc(garmin_tokens)           │
│                                           · access_tokens:   token_hash → account         │
│                                                                                          │
│  Garmin login (garminconnect library)     Worker manager (per Garmin account)             │
│   web form → login → MFA → tokens          ensure-running → reverse-proxy /mcp            │
│                       │                              │                                     │
│                       ▼                              ▼                                     │
│           write tokens to                127.0.0.1:<port> ──┐                              │
│           /data/users/<id>/tokens                           │                              │
└──────────────────────────────────────────────────────────────┼──────────────────────────┘
                                                                ▼
                          ┌───────────────────────────────────────────────────┐
                          │  garmin-mcp worker  (UNMODIFIED, 1 process / account)│
                          │  GARMIN_MCP_TRANSPORT=streamable-http                │
                          │  GARMIN_MCP_HOST=127.0.0.1   GARMIN_MCP_PORT=<port>  │
                          │  GARMINTOKENS=/data/users/<id>/tokens                │
                          └───────────────────────────────────────────────────┘
                                                                │
                                                                ▼
                                                       connect.garmin.com
```

**Principles**
- `garmin_mcp` is a black box: the gateway interacts only via (a) its HTTP MCP
  endpoint and (b) its documented env vars. No source edits, no internal imports.
- Workers bind **`127.0.0.1` only** — never exposed; only the gateway reaches them.
- Workers start **lazily** (after Garmin login has provisioned tokens, on the
  first `/mcp` request) and are **idle-evicted**.

## Components

### 1. OAuth 2.1 layer (mirrors rohlik)

Endpoints:

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | — | Landing page (operator name/email) |
| GET | `/.well-known/oauth-authorization-server` | — | OAuth metadata (discovery) |
| POST | `/oauth/register` | — | Dynamic Client Registration → `client_id` |
| GET | `/oauth/authorize` | — | Garmin login form (email + password) |
| POST | `/oauth/authorize` | — | Garmin login (+ MFA step) → auth code redirect |
| POST | `/oauth/token` | Client | Exchange auth code for Bearer token (verifies PKCE) |
| POST | `/mcp` | Bearer | Proxy MCP request to the user's worker |
| GET | `/mcp` | Bearer | SSE stream from the user's worker |
| DELETE | `/mcp` | Bearer | Close MCP session |
| GET | `/healthz` | — | Gateway liveness probe |

- **PKCE S256 required** (OAuth 2.1); `plain` rejected.
- Auth codes are one-time, 10-minute expiry, deleted on consume, bound to
  `client_id` + `redirect_uri` + `code_challenge`.
- `redirect_uri` validated against the registered client's allowlist on both
  authorize and token.

### 2. Garmin login sub-flow (with MFA)

Uses `garminconnect` with `return_on_mfa=True` — the same mechanism
`garmin-mcp-auth` uses, but driven from the web form:

```
1. POST /oauth/authorize  (garmin_email, garmin_password, + CSRF, + OAuth params)
       g = Garmin(email, password, return_on_mfa=True);  r1, r2 = g.login()
         ├── r1 == "needs_mfa":
         │     store (g, r2) in-memory under random login_id (TTL 5 min)
         │     DISCARD password now
         │     render MFA page with hidden login_id (+ CSRF, + OAuth params)
         │
         │   2. POST /oauth/authorize (login_id, mfa_code, ...)
         │        g.resume_login(r2, mfa_code)
         │
         └── login OK (either path):
               g.garth.dump(<tmp>) → garmin_tokens.json (OAuth1 + OAuth2)
               upsert garmin_accounts(garmin_user_key, enc(tokens))
               create one-time auth code bound to garmin_user_key + PKCE
               redirect to redirect_uri?code=...&state=...
```

- Accounts **without MFA** skip the second step (`login()` succeeds directly).
- **Discarded:** the Garmin password (never written to disk); the in-memory MFA
  login state (TTL 5 min, deleted after use).
- **Persisted:** the Garmin tokens (encrypted), keyed by `garmin_user_key` (the
  normalized, lowercased Garmin login email captured from the form).

### 3. Worker manager (per Garmin account)

In-memory registry, keyed by `garmin_user_key` (NOT by Bearer token — so a
user's phone and desktop share one warm worker).

`WorkerHandle = { garmin_user_key, port, process, last_active, status }`

`ensure_worker(garmin_user_key)` — called at the start of every `/mcp` request,
guarded by a per-account asyncio lock so we never spawn two workers for one user:

```
running and /healthz OK?  → bump last_active, return port
otherwise:
   1. decrypt tokens from SQLite → write /data/users/<id>/tokens/garmin_tokens.json (0600)
   2. allocate a free port from WORKER_PORT_RANGE (e.g. 9000–9099)
   3. spawn `garmin-mcp` with env:
        GARMIN_MCP_TRANSPORT=streamable-http
        GARMIN_MCP_HOST=127.0.0.1
        GARMIN_MCP_PORT=<port>
        GARMINTOKENS=/data/users/<id>/tokens
   4. poll http://127.0.0.1:<port>/healthz (timeout ~15 s)
        ok        → store handle, return port
        exited    → tokens invalid → surface "reconnect" error
```

Routing `/mcp` (POST/GET/DELETE) is rohlik's `proxy.ts` translated to Python,
target `http://127.0.0.1:<port>/mcp`: forwards `Mcp-Session-Id` and `Accept`,
streams `text/event-stream`, 30 s timeout (→ 504), 1 MB body limit (→ 413). One
FastMCP worker handles multiple concurrent MCP sessions, so multiple devices of
the same user each get their own `Mcp-Session-Id` against the shared worker.

Lifecycle:
- **Idle reaper** (background task): kill workers idle > `WORKER_IDLE_TTL`
  (default 15 min); free the port.
- **Cap** at `MAX_WORKERS` (default 10); LRU-evict the least-recently-used when over.
- **Cold start** ~1–2 s (Garmin token validation at startup) on first request
  after idle.
- **Crash:** a dead worker is respawned on the next request; repeated failure
  (invalid tokens) → "session expired, reconnect".
- **Gateway restart:** the registry is ephemeral; workers respawn lazily from
  persisted tokens. User identity and tokens persist in SQLite (see below).

### 4. Data model + storage

SQLite on the persistent `/data` volume — this is the **durable record of who
has authenticated** and survives container restarts.

```
garmin_accounts                    -- one row per Garmin account
  garmin_user_key   TEXT PRIMARY KEY     -- the normalized (lowercased) Garmin login email,
                                         -- captured from the authorize form
  garmin_tokens_enc TEXT NOT NULL        -- AES-256-GCM(garmin_tokens.json)
  created_at, updated_at

access_tokens                      -- one row per device/authorization
  token_hash      TEXT PRIMARY KEY       -- SHA-256(Bearer); never plaintext
  garmin_user_key TEXT NOT NULL          -- → garmin_accounts
  client_id       TEXT
  created_at, last_used

oauth_clients                      -- Dynamic Client Registration
  client_id TEXT PRIMARY KEY, client_secret_hash TEXT,
  redirect_uris TEXT (JSON), client_name TEXT, created_at

oauth_codes                        -- one-time, 10 min
  code_hash TEXT PRIMARY KEY, client_id, redirect_uri,
  code_challenge, code_challenge_method,
  garmin_user_key,                       -- which account the code grants
  expires_at, created_at
```

- Encryption: `cryptography` AES-256-GCM, key = SHA-256(`GATEWAY_SECRET`),
  random 12-byte nonce, stored `nonce:ciphertext` (hex). `cryptography` is
  already a transitive dependency of `garminconnect`.
- DB file `0600`; per-user token dirs `0700`, token files `0600`.
- The DB is **useless without `GATEWAY_SECRET`** — a feature for backups.

### 5. Security hardening

- Garmin **password never persisted** (core).
- **PKCE S256** required; one-time, expiring auth codes.
- **CSRF** one-time token on both the login and MFA forms.
- **redirect_uri allowlist**, strict match.
- **Rate limiting:** OAuth endpoints 20/min/IP; `/mcp` 60/min/token; unauth
  30/min/IP; **login POST stricter (~5/min/IP)** to avoid hammering Garmin and
  tripping its Cloudflare/lockout.
- **Security headers:** CSP, HSTS, X-Frame-Options DENY, X-Content-Type-Options
  nosniff, Referrer-Policy.
- Workers bind `127.0.0.1` only; 1 MB body limit; 256-bit Bearer tokens.
- **Logs carry no secrets** — no password/token/MFA; at most a token-hash prefix.

## Data flow (end to end)

1. User adds `https://<domain>/mcp` as a remote MCP server in Claude.
2. Claude discovers OAuth metadata, registers a client (DCR), opens
   `/oauth/authorize` with PKCE.
3. User enters Garmin email + password (+ MFA code if prompted).
4. Gateway logs in via `garminconnect`, stores encrypted tokens, discards the
   password, issues a one-time auth code, redirects back.
5. Claude exchanges the code at `/oauth/token` (PKCE verifier) → Bearer token.
   Gateway stores `SHA-256(token) → garmin_user_key`.
6. Claude calls `/mcp` with the Bearer token. Gateway authenticates, ensures the
   user's worker is running, and reverse-proxies MCP traffic to it.
7. Worker talks to `connect.garmin.com` with that user's tokens and returns tool
   results, streamed back through the gateway.

## Error handling

| Condition | Behavior |
|---|---|
| Invalid/expired Bearer | 401 |
| Bad Garmin credentials | Re-render login form with error |
| MFA required | Render MFA step; resume with code |
| MFA state expired (>5 min) | Restart login from the form |
| Worker fails to start (bad tokens) | Surface "session expired, reconnect" |
| Worker timeout (>30 s) | 504 |
| Body > 1 MB | 413 |
| Rate limit exceeded | 429 |
| Garmin rate-limited / unreachable | Friendly message (worker's own `_GarminProxy` text) |

## Deployment

- **Single Docker image:** Python 3.12 + `uv`; installs the gateway and
  `garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp@<pinned-commit>`. The
  gateway spawns `garmin-mcp` child processes inside the same container.
- **`docker-compose.yml`:** one `gateway` service, persistent volume mounted at
  `/data` (SQLite DB + per-user token dirs), env from `.env`, **`init: true`**
  (proper reaping of the many worker subprocesses), `restart: unless-stopped`.
- **nginx in front** (operator-managed, same as rohlik): TLS termination +
  public domain, proxies to the gateway on localhost. `PUBLIC_URL` = the public
  HTTPS URL.
- **`.env` keys:** `GATEWAY_SECRET` (≥32 chars), `PUBLIC_URL`, `PORT`
  (default 8080), `DATA_DIR` (default `/data`), `GARMIN_MCP_REF` (pinned commit),
  `WORKER_PORT_RANGE`, `WORKER_IDLE_TTL`, `MAX_WORKERS`, `OPERATOR_NAME`,
  `OPERATOR_EMAIL`.
- **Backups:** back up `/data`; keep `GATEWAY_SECRET` separately (DB is encrypted
  at rest and useless without it).

## Testing strategy

Implementation will follow TDD.

- **Unit:** crypto roundtrip; token hashing; PKCE S256 verification;
  redirect_uri allowlist; rate limiter; auth-code one-time-use + expiry.
- **OAuth flow (integration):** DCR → authorize → token, with **`garminconnect`
  mocked** (success / `needs_mfa` / failure). Verify PKCE binding, code
  one-time-use, MFA resume, MFA-state TTL.
- **Worker manager:** against a **fake worker** (a tiny stub HTTP server that
  mimics `/mcp` + `/healthz`) — spawn/reuse/idle-eviction/respawn-after-crash/
  port allocation/per-account lock/cap+LRU. No real Garmin.
- **Proxy:** SSE pass-through; `Mcp-Session-Id` forwarding; timeout → 504; body
  limit → 413.
- **E2E smoke (manual, opt-in):** real Garmin account; authorize in a browser;
  add the URL in Claude mobile; run a tool. Outside CI (needs real creds + MFA).

## Open questions / future

- Optional small admin view (list accounts / revoke a device) — deferred; for now
  admin is direct DB/CLI access.
- Token pre-warm (keep workers hot) — deferred; idle eviction is the default.
- Multi-region / horizontal scale — out of scope for a small circle (single node,
  in-memory worker registry).
