# Architecture

The module-by-module reference for the gateway. `CLAUDE.md` carries the
one-line map and the cross-cutting invariants; this file carries the detail.

Vocabulary (connector, adapter, forward strategy, worker, upstream) is defined
in [`CONTEXT.md`](../CONTEXT.md).

The end-to-end request flow lives in
[`CLAUDE.md` → Architecture](../CLAUDE.md#architecture) and is deliberately
**not** repeated here — one copy, so the two can't drift.

## Modules

`src/missingmcp/`, in dependency order — each has one responsibility and
composes through small explicit contracts.

### `config.py`

`Config` frozen dataclass + `load_config(env)`. Single source of all tunables
(read from env). Refuses to start without a valid `GATEWAY_SECRET`.

### `log.py`

Structured JSON logging to stdout (`log` / `log_warn` / `log_error`), plus
`_StructuredHandler` bridging stdlib/uvicorn/warnings records into the same
stream (`event=stdlib-log`; uvicorn runs with `log_config=None`). Callers must
never pass secrets; the runtime supplies timestamps.

### `store.py`

SQLite schema + AES-256-GCM crypto + token hashing + CRUD, **adapter-keyed**.
Tables: `accounts` (encrypted per-account blob, PK `(adapter, account_key)`),
`access_tokens` (Bearer hash → `(adapter, account_key)`), `oauth_clients` (DCR,
per adapter), `oauth_codes` (one-time PKCE, per adapter), `tool_usage`
(per-account metrics), `subscribers` (newsletter opt-in email, PK email) and
`suggestions` (connector-request log). A guarded `PRAGMA user_version` 0→1
migration rewrites the pre-adapter Garmin schema in place (ciphertext verbatim).
Encryption key = `SHA-256(GATEWAY_SECRET)`.

Data-hygiene ops (driven by the app lifespan loop): `cleanup_orphan_clients`
(0-token DCR registrations older than the cutoff) and `purge_adapter` (full
off-boarding of one adapter's rows across every table), alongside the
pre-existing `cleanup_expired_codes` / `cleanup_expired_tokens`.

### `security.py`

PKCE S256 verify, redirect_uri allowlist, `CsrfStore`, sliding-window
`RateLimiter`, security headers, `read_body_limited`.

### `pages.py`

`render_page(fragment, title, desc)`: wraps a content fragment from `templates/`
in the shared site chrome (`templates/_layout.html` — header, nav, footer, the
whole stylesheet), so every page (home, connector landings, sign-in/MFA forms)
is one visual site. Fragment placeholders (`{PUBLIC_URL}`, `{ERROR}`,
`{OAUTH_FIELDS}`, …) survive wrapping for the caller to fill.

`templates/garmin.html` is the connector-page template (hero → about → data →
connect → tips → under the hood → tools); its "All tools" section is generated
by `scripts/gen_garmin_tools.py` between `GENERATED:TOOLS` markers.

### `adapters/base.py`

The adapter contract: `Adapter` protocol (incl. `landing_template`), the three
forward strategies as protocols — `WorkerForward` (subprocess), `RemoteForward`
(`upstream_url` + `headers(blob)`), and `LocalForward` (`handle(conn,
account_key, blob, body)`, raises `SessionExpired` when credentials are beyond
saving) — duck-typed dispatch via `is_remote` / `is_local`, plus the
upstream-OAuth login shape (`is_upstream_oauth(adapter)`,
`authorize_redirect_url` / `handle_callback`) alongside the form-login
`LoginOk` / `SecondFactorNeeded` results and `LoginError` / `SecondFactorError`.
The seam between the core and upstream services (spec 2026-07-05).

### `adapters/garmin/`

`login.py` is the thin `garminconnect` wrapper (`start_login` MFA-aware with
transient-block retry, `resume_login`, `verify_tokens`); `GarminAdapter` owns
form-field names, error copy, account-key normalization and the second-factor
state; `GarminWorkerForward` owns the worker CLI/env contract + token
materialization.

Registry: `adapters.build_adapters(config)`. `adapters.RETIRED_ADAPTERS` is the
explicit frozenset of retired adapters the cleanup loop purges — see
[ADR-0001](adr/0001-retired-adapter-cleanup.md) for why retirement is never
inferred from registry-absence.

### `adapters/whoop/`

`api.py` is the WHOOP v2 HTTP client: upstream-OAuth code exchange plus
gateway-owned rotating token refresh, serialized per account and persisted
before use. `mcp.py` is the hand-rolled, stateless JSON-RPC MCP server (`TOOLS`
table + dispatch) that *is* `/whoop/mcp`, running in-process. `__init__.py`'s
`WhoopAdapter` owns the upstream-OAuth login shape (`authorize_redirect_url` /
`handle_callback`) and wraps `WhoopLocalForward`. Registered only when both
`WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` are set (`adapters.build_adapters`).

### `oauth.py`

One cohesive module covering metadata (RFC 8414), DCR (RFC 7591), the
`/<adapter>/oauth/authorize` form + adapter login + MFA two-step, and
`/<adapter>/oauth/token` exchange. `AuthState` holds the in-memory MFA-pending
map (TTL 300s).

### `backup.py`

Off-box DB backups: SQLite backup-API snapshot uploaded to an S3-compatible
bucket (dependency-free SigV4 signer over httpx), weekday-rotated keys
(`db/gateway-<mon..sun>.db`). Driven by the app lifespan loop
(`Backup.enabled` / `due` / `run`); `run` never raises. Disabled unless all
`BACKUP_S3_*` are set.

### `report.py`

Daily user-stats Slack report: yesterday's per-connector new/active/total users
+ a 7-day new count, computed straight from the DB
(`store.new_accounts_between` / `active_accounts_between` /
`total_accounts_by_adapter`). Driven by the app lifespan loop
(`DailyReport.enabled` / `due` / `run`, mirrors `backup.py`; `run` opens its own
read-only conn and never raises), posts once a day at `DAILY_REPORT_HOUR` (local
`DAILY_REPORT_TZ`, default 08:00 Europe/Prague). Disabled unless
`SLACK_WEBHOOK_URL` is set. The same `build_report` / `render_slack` back
`scripts/daily_report.py` (print / `--post`). A redeploy after the hour skips
that day (process-local) to avoid duplicate posts.

Complementing it, a separate *external* job — `scripts/hourly_digest.py` run by
`.github/workflows/hourly-digest.yml` — posts the *log-derived* hourly health
digest (Railway logs API → summary + liveness probe → Slack, silent unless
anomaly/probe-fail or the daily heartbeat). It's standalone (httpx + stdlib, does
NOT import this package) because the app can't read its own stdout logs; see
README → Monitoring.

### `usage.py`

The public usage meter: `UsageMeter.snippet(adapter)` renders the
"N people used this in the last 30 days" line the landing pages show as social
proof. Aggregate only (one count per adapter, never per-user activity), counted
by `store.active_accounts_between` over a 30-day rolling window (protocol
traffic excluded, same rule as the daily report), and empty below `MIN_COUNT`
(10) so an immature connector shows nothing rather than an embarrassing count.
Counts live in a process-local TTL cache (`CACHE_TTL` 600 s); `app.py` fills
the `{USAGE_METER_<ADAPTER>}` placeholders per request, matching them by
pattern so a card for a not-configured adapter still gets its placeholder
cleared.

### `telemetry.py`

PostHog telemetry (design:
`docs/superpowers/specs/2026-07-20-posthog-telemetry-design.md`): the official
`posthog` SDK client (module-level, like `log.py`), `capture` / `identify`
wrappers, the posthog-js head/bootstrap for pages, and an OTLP tee of the
structured log stream into PostHog Logs (hooked into `log._emit` via
`log.set_sink` — NOT the root logger; export-path loggers are excluded to
prevent feedback loops). Env-gated by `POSTHOG_API_KEY` (unset ⇒ every function
is a no-op) and fire-and-forget throughout — telemetry must never block a
request or crash the process. Event names are canonical `$mcp_*` (PostHog's
MCP-analytics wire contract) plus a lean snake_case funnel/conversion set — both
are a stable schema like the log events.

### `workers.py`

`WorkerManager(config, forward)`: per-account `asyncio.Lock` (no double-spawn),
lazy spawn, `/healthz` poll, idle reaper, LRU cap; dirs `0700` are
manager-owned, credential files come from `forward.materialize` (`0600`).
`spawn` is injectable for tests. Worker stdout/stderr is pumped line-by-line
into the structured log (`event=worker-log`, `account` attr, ERROR/Traceback
lines elevated) — no per-user `worker.log` files on the volume.

**Port hygiene** (reliability ticket 12): `_alloc_port` round-robins through
the range (never lowest-free-first — that hands the next spawn exactly the
port its own `_enforce_cap` eviction just freed), and a terminated worker's
port *cools down* until its process is observed dead, because a SIGTERMed
uvicorn keeps answering `/healthz` for a moment and a fresh spawn must never
be validated against its dying predecessor's listener. SIGKILL escalation
after `_COOLING_KILL_S`, hard expiry after `_COOLING_MAX_S`, so a zombie
can't shrink the pool. The proxy adds one `ConnectError` retry per forward
(re-running `ensure_worker`) as belt-and-braces.

**Token read-back** (the persist-before-use rule, worker edition): the worker
rewrites its credential file when the upstream rotates tokens (garth does, on
Garmin's refresh-token rotation), so the manager persists that file back to the
store via the injected `persist(key, blob)` callback whenever it differs from
the last store state this process knows (`_persisted`, seeded by every
materialize). Capture points: the periodic `persist_rotated()` (lifespan loop,
under the per-account lock, skipping held locks), the reap/evict paths (the
account is about to leave the registry), `shutdown()` (deploys are frequent),
and `ensure_worker`'s respawn path — which then materializes the recovered
rotation instead of the caller's now-stale blob. A torn (unparseable) file is
never persisted (`forward.read_back` → None, retried next tick), and a fresh
process with no baseline trusts the store over the disk — a differing file may
predate a re-login, so pre-fix drift is repaired only by an explicit backfill.
Events: `worker-tokens-persisted` / `worker-tokens-persist-failed` (with
`trigger`).

### `proxy.py`

`authenticate` (Bearer + rate limits) and `handle_mcp`: a shared core (body
limit, blob fetch, usage, header threading, streaming forward, timeout→504) plus
strategy dispatch via `is_remote` / `is_local`:

- **local** — 405s on GET/DELETE (stateless, no sessions) and calls
  `forward.handle(conn, account_key, blob, body)` in-process, mapping
  `SessionExpired` to a re-auth 401 (event `local-forward-auth-stale`)
- **worker** — calls `ensure_worker`; every failure becomes a re-auth 401, but
  they are logged apart, because only one of them is routine. A worker that exits
  **cleanly (rc 0)** during startup has rejected the account's stored credentials
  — for `garmin_mcp`, "OAuth tokens not found … Exiting." on stale tokens
  (`WorkerCredentialsRejected` → events `worker-exited-early` +
  `worker-forward-auth-stale`, info; the user re-signs-in and no operator is
  needed). Everything else is a genuine fault that keeps `worker-start-failed` at
  error level: a spawn failure, an exhausted port range, a worker that stayed
  alive and never answered `/healthz` (`worker-unhealthy`), and — importantly — a
  worker that died **non-zero or on a signal** (`worker-died`, e.g. rc 1 on a
  traceback or 137 on an OOM kill), which must never be mistaken for stale
  credentials or a crash-loop would go silent. The ops alert and
  `hourly_digest.py`'s `SELF_HEAL_EVENTS` both key off that split
- **remote** — injects `forward.headers(blob)` and maps upstream 401/403 to the
  same re-auth 401 (event `remote-forward-auth-stale`)

All three — plus a missing account blob — funnel through `_reauth_required`: a
**401 carrying the RFC 9728 `WWW-Authenticate: Bearer resource_metadata=…`
challenge** (body `error: invalid_token`) so the MCP client re-runs
authorization and the user self-heals with a fresh sign-in. A 502 was a dead end
clients retry forever (`docs` / `.scratch/oauth-client-lifecycle`).

Every completed forward logs `mcp-response` (account, tool, status,
`ttfb_ms` / `total_ms` / `bytes`) — the per-request latency record.

A stream the upstream aborts mid-body (`httpx.RemoteProtocolError` /
`ReadError` / `ReadTimeout` inside the proxied iterator — for MCP that's a
routine session teardown under an open listen stream) ends the response and
logs one **warn** `mcp-stream-interrupted` (adapter, account, tool, error
type, bytes sent) instead of escaping into the ASGI stack as an ERROR
traceback; the worker's mirror stdout line ("ASGI callable returned without
completing response") is likewise kept at info by the pump filter. Warn, not
silent: a surge of interrupted POST tool calls would be user-facing.

### `app.py`

`build_app(config)` wires routes + security-headers middleware + shared
singletons (db conn, one `WorkerManager` **per worker-based adapter** — remote
and local adapters get none — `AuthState`, `RateLimiter`), a per-adapter landing
route rendered from `adapter.landing_template`, plus two unauthenticated public
opt-in endpoints — `POST /subscribe` and `POST /suggest` — capturing home-page
signups/suggestions (rate-limited + honeypot + email-format check; storage only,
no email sent).

The lifespan periodically reaps idle workers (all managers) and runs data
hygiene — cleans expired codes/tokens, sweeps abandoned OAuth clients (0 tokens,
unused — `last_seen`, stamped by `get_client` on every authorize/token use —
for longer than `config.orphan_client_ttl`), and fully purges any
`adapters.RETIRED_ADAPTERS` data (`_run_data_cleanup`; see
[ADR-0001](adr/0001-retired-adapter-cleanup.md)). `main()` is the `missingmcp`
console entrypoint.
