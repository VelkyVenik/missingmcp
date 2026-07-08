# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-user, OAuth 2.1–protected **remote MCP gateway** that lets a small trusted circle each connect their own upstream-service accounts to Claude (mobile/desktop/web). The gateway terminates OAuth, performs the adapter-specific login — a credential form (garmin) or a redirect to the upstream's own OAuth (whoop: `/whoop/oauth/callback`) — stores per-account encrypted blobs, and forwards `/<adapter>/mcp` via one of three strategies: **worker** (garmin — spawns + reverse-proxies to a per-user subprocess of the **unmodified** `garmin_mcp` worker, `github.com/Taxuspt/garmin_mcp`), **remote** (no subprocess; forwards to a hosted upstream MCP, injecting the account's credentials as headers), or **local** (whoop — no subprocess, no shared upstream; the MCP server runs in-process, see `adapters/whoop/mcp.py`). No in-tree adapter uses the remote strategy today — rohlik used it until Rohlík shipped its own OAuth MCP (2026-07); the strategy stays covered by `tests/test_remote_forward.py` via a stub adapter.

The canonical design and the task-by-task implementation plan live in `docs/superpowers/specs/` and `docs/superpowers/plans/` — read them for rationale and the full data flow, but treat them as dated design records: the 2026-07-05 multi-adapter spec still describes a rohlik adapter that was implemented and then retired (2026-07-06, Rohlík ships its own OAuth MCP) — don't re-add it. Operator-facing docs (env-var reference, monitoring, deploy checklist) live in `README.md`; operational scripts (`status`, `revoke`, `usage`) live in `scripts/` and are documented in README → Monitoring.

## Commands

```bash
# Tests — the `--extra dev` is REQUIRED: pytest lives in [project.optional-dependencies].dev,
# so plain `uv run pytest` fails with "no module named pytest".
uv run --extra dev pytest -q                          # full suite
uv run --extra dev pytest tests/test_oauth.py -v      # one file
uv run --extra dev pytest tests/test_oauth.py::test_metadata_shape -v   # one test

# Run the gateway locally (no Garmin needed to exercise the OAuth surface).
# DATA_DIR defaults to /data (not writable locally) — point it somewhere writable.
# GATEWAY_SECRET must be >=32 chars AND must not start with "change-me" (startup guard).
# To exercise the full /<adapter>/mcp path locally, also set GARMIN_MCP_CMD (garmin-mcp isn't on
# PATH): GARMIN_MCP_CMD="uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp"
GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 \
  DATA_DIR=./.localdata uv run missingmcp

# After changing adapters/whoop/mcp.py's TOOLS table, regenerate the landing
# page's tool listing:
python scripts/gen_whoop_tools.py

# Production (missingmcp.com) runs on Railway, built from the Dockerfile, and
# auto-deploys on every push to main — pushing = deploying. Verify after push:
# railway deployment list --json. (Self-host: plain `docker run` — see README.)
```

There is no separate lint step configured.

## Hard constraints

- **Never modify or import `garmin_mcp`.** Interact with it *only* as a black box via its documented CLI entrypoint (`garmin-mcp`) and env vars (`GARMIN_MCP_TRANSPORT`, `GARMIN_MCP_HOST`, `GARMIN_MCP_PORT`, `GARMINTOKENS`). No source edits, no importing its internal modules.
- **Pin `GARMIN_MCP_REF`** to a reviewed commit SHA in production (the `main` default is a floating ref — supply-chain risk). After bumping the pin, run `python scripts/gen_garmin_tools.py` — it regenerates the "All tools" section of `templates/garmin.html` from the new ref.
- **Python 3.12** (matches the worker's interpreter). All source under `src/missingmcp/`, all tests under `tests/`.

## Architecture

Request flow (one user, one device):

```
Claude → OAuth 2.1 (DCR → /<adapter>/oauth/register → /<adapter>/oauth/authorize → /<adapter>/oauth/token, PKCE S256, RFC 8414 discovery at /.well-known/oauth-authorization-server/<adapter>)
       → adapter-specific login (garmin: garminconnect, password discarded, tokens kept; whoop: redirect to WHOOP's own OAuth, callback at /whoop/oauth/callback)
       → encrypted blob in SQLite, keyed by (adapter, account_key)
       → on POST /<adapter>/mcp, forward strategy (RFC 9728 discovery at /.well-known/oauth-protected-resource/<adapter>/mcp):
           worker (garmin): ensure the user's worker subprocess (127.0.0.1:<port>) → reverse-proxy
           remote (no in-tree adapter today): stream-forward to forward.upstream_url with forward.headers(blob) injected
           local (whoop): forward.handle(conn, account_key, blob, body) runs the MCP server in-process — no subprocess, no upstream_url
```

Modules (`src/missingmcp/`), in dependency order — each has one responsibility and composes through small explicit contracts:

- **`config.py`** — `Config` frozen dataclass + `load_config(env)`. Single source of all tunables (read from env). Refuses to start without a valid `GATEWAY_SECRET`.
- **`log.py`** — structured JSON logging to stdout (`log` / `log_warn` / `log_error`), plus `_StructuredHandler` bridging stdlib/uvicorn/warnings records into the same stream (`event=stdlib-log`; uvicorn runs with `log_config=None`). NOTHING may write plain text to stderr — Railway classifies it as error-severity. Callers must never pass secrets; the runtime supplies timestamps.
- **`store.py`** — SQLite schema + AES-256-GCM crypto + token hashing + CRUD, **adapter-keyed**. Tables: `accounts` (encrypted per-account blob, PK `(adapter, account_key)`), `access_tokens` (Bearer hash → `(adapter, account_key)`), `oauth_clients` (DCR, per adapter), `oauth_codes` (one-time PKCE, per adapter), `tool_usage` (per-account metrics), `subscribers` (newsletter opt-in email, PK email) and `suggestions` (connector-request log). A guarded `PRAGMA user_version` 0→1 migration rewrites the pre-adapter Garmin schema in place (ciphertext verbatim). Encryption key = `SHA-256(GATEWAY_SECRET)`. Data-hygiene ops (driven by the app lifespan loop): `cleanup_orphan_clients` (0-token DCR registrations older than the cutoff) and `purge_adapter` (full off-boarding of one adapter's rows across every table), alongside the pre-existing `cleanup_expired_codes`/`cleanup_expired_tokens`.
- **`security.py`** — PKCE S256 verify, redirect_uri allowlist, `CsrfStore`, sliding-window `RateLimiter`, security headers, `read_body_limited`.
- **`pages.py`** — `render_page(fragment, title, desc)`: wraps a content fragment from `templates/` in the shared site chrome (`templates/_layout.html` — header, nav, footer, the whole stylesheet), so every page (home, connector landings, sign-in/MFA forms) is one visual site. Fragment placeholders (`{PUBLIC_URL}`, `{ERROR}`, `{OAUTH_FIELDS}`, …) survive wrapping for the caller to fill. `templates/garmin.html` is the connector-page template (hero → about → data → connect → tips → under the hood → tools); its "All tools" section is generated by `scripts/gen_garmin_tools.py` between `GENERATED:TOOLS` markers.
- **`adapters/base.py`** — the adapter contract: `Adapter` protocol (incl. `landing_template`), the three forward strategies as protocols — `WorkerForward` (subprocess), `RemoteForward` (`upstream_url` + `headers(blob)`), and `LocalForward` (`handle(conn, account_key, blob, body)`, raises `SessionExpired` when credentials are beyond saving) — duck-typed dispatch via `is_remote`/`is_local`, plus the upstream-OAuth login shape (`is_upstream_oauth(adapter)`, `authorize_redirect_url`/`handle_callback`) alongside the form-login `LoginOk`/`SecondFactorNeeded` results and `LoginError`/`SecondFactorError`. The seam between the core and upstream services (spec 2026-07-05).
- **`adapters/garmin/`** — `login.py` is the thin `garminconnect` wrapper (`start_login` MFA-aware with transient-block retry, `resume_login`, `verify_tokens`); `GarminAdapter` owns form-field names, error copy, account-key normalization and the second-factor state; `GarminWorkerForward` owns the worker CLI/env contract + token materialization. Registry: `adapters.build_adapters(config)`; `adapters.RETIRED_ADAPTERS` is the **explicit** frozenset of deliberately-dropped adapters (currently `{"rohlik"}`) that the cleanup loop fully purges — retirement is never inferred from registry-absence, since a missing env var (e.g. `WHOOP_*`) would then masquerade as a retirement and delete live data (`docs/adr/0001`).
- **`adapters/whoop/`** — `api.py` is the WHOOP v2 HTTP client: upstream-OAuth code exchange plus gateway-owned rotating token refresh, serialized per account and persisted before use; `mcp.py` is the hand-rolled, stateless JSON-RPC MCP server (`TOOLS` table + dispatch) that *is* `/whoop/mcp`, running in-process; `__init__.py`'s `WhoopAdapter` owns the upstream-OAuth login shape (`authorize_redirect_url`/`handle_callback`) and wraps `WhoopLocalForward`. Registered only when both `WHOOP_CLIENT_ID`/`WHOOP_CLIENT_SECRET` are set (`adapters.build_adapters`).
- **`oauth.py`** — one cohesive module covering metadata (RFC 8414), DCR (RFC 7591), the `/<adapter>/oauth/authorize` form + adapter login + MFA two-step, and `/<adapter>/oauth/token` exchange. `AuthState` holds the in-memory MFA-pending map (TTL 300s).
- **`backup.py`** — off-box DB backups: SQLite backup-API snapshot uploaded to an S3-compatible bucket (dependency-free SigV4 signer over httpx), weekday-rotated keys (`db/gateway-<mon..sun>.db`). Driven by the app lifespan loop (`Backup.enabled`/`due`/`run`); `run` never raises. Disabled unless all `BACKUP_S3_*` are set.
- **`workers.py`** — `WorkerManager(config, forward)`: per-account `asyncio.Lock` (no double-spawn), lazy spawn, `/healthz` poll, idle reaper, LRU cap; dirs `0700` are manager-owned, credential files come from `forward.materialize` (`0600`). `spawn` is injectable for tests. Worker stdout/stderr is pumped line-by-line into the structured log (`event=worker-log`, `account` attr, ERROR/Traceback lines elevated) — no more per-user `worker.log` files on the volume.
- **`proxy.py`** — `authenticate` (Bearer + rate limits) and `handle_mcp`: a shared core (body limit, blob fetch, usage, header threading, streaming forward, timeout→504) plus strategy dispatch via `is_remote`/`is_local` — local path 405s on GET/DELETE (stateless, no sessions) and calls `forward.handle(conn, account_key, blob, body)` in-process, mapping `SessionExpired` to the same 502 shape (event `local-forward-auth-stale`); worker path calls `ensure_worker` (start-failure→502 `<adapter>_session_expired`); remote path injects `forward.headers(blob)` and maps upstream 401/403 to the same 502 shape (event `remote-forward-auth-stale`). Every completed forward logs `mcp-response` (account, tool, status, `ttfb_ms`/`total_ms`/`bytes`) — the per-request latency record.
- **`app.py`** — `build_app(config)` wires routes + security-headers middleware + shared singletons (db conn, one `WorkerManager` **per worker-based adapter** — remote and local adapters get none, `AuthState`, `RateLimiter`), a per-adapter landing route rendered from `adapter.landing_template`, plus two unauthenticated public opt-in endpoints — `POST /subscribe` and `POST /suggest` — capturing home-page signups/suggestions (rate-limited + honeypot + email-format check; storage only, no email sent), and a lifespan that periodically reaps idle workers (all managers) and runs data hygiene — cleans expired codes/tokens, sweeps abandoned OAuth clients (0 tokens, older than `config.orphan_client_ttl`), and fully purges any `adapters.RETIRED_ADAPTERS` data (`_run_data_cleanup`; see `docs/adr/0001`). `main()` is the `missingmcp` console entrypoint.

## Cross-cutting invariants (easy to break, hard to see from one file)

- **`account_key`** = the normalized **lowercased login email**, scoped by `adapter`. `(adapter, account_key)` is the join key across every table *and* (with `account_key` alone) the worker registry. A Bearer token carries its `adapter`; the proxy rejects a token used on a different adapter's `/mcp`.
- **Secret handling:** the Garmin **password is never persisted or logged** (held in a local, `del`-ed right after `start_login`). **Bearer tokens and client secrets are stored only as SHA-256 hashes.** Garmin tokens are AES-256-GCM encrypted at rest (`token files 0600`, `dirs 0700`). Logs carry at most an 8-char hash prefix. (A remote-strategy adapter may need to keep login credentials in its blob — the upstream authenticates every request — but they still live only inside the encrypted blob, never logged, never materialized to files.)
- **WHOOP refresh tokens rotate on every use:** WHOOP invalidates the old refresh token the instant a new one is issued, so only the gateway ever refreshes (never a worker — there is no whoop worker), refreshes are serialized per account under an `asyncio.Lock` (`WhoopApi.ensure_fresh`), and the rotated blob is always persisted to the store *before* it's used for a request. Refresh requests carry `scope: offline` — the scope WHOOP requires to keep issuing refresh tokens at all.
- **Verify-then-persist:** in `oauth.py`, `adapter.verify` is the only "expectedly failing" step and gates `_finish` (which does upsert + code-mint + redirect) on **every** authorize path. A login/verify failure re-renders the form; a wrong MFA code re-prompts. Don't move `adapter.verify` back into `_finish`.
- **Blocking adapter sign-in runs off the event loop, capped.** `adapter.start_login` / `resume_second_factor` / `verify` do **synchronous** network I/O (garminconnect); `oauth.authorize_post` must call them through `_bounded` (`asyncio.to_thread` + `wait_for(config.login_timeout)`), never directly — a direct call freezes the single-node event loop for every user, and a Garmin login that's being rate-limited can block ~2 minutes (a 125s POST was observed). A timeout re-renders the form (`*-timeout` log events); the abandoned worker thread finishes on its own.
- **PKCE S256 only** (`plain` rejected); **`redirect_uri` exact-match allowlist** enforced on `authorize_get`, the login branch *and* the MFA branch of `authorize_post`, and at `/token`.
- **Workers bind `127.0.0.1` only** — only the gateway reaches them. TLS terminates in front of the gateway (the Railway edge in production; a self-hoster brings their own proxy).
- **Process-local state** (worker registry, `AuthState`, `CsrfStore`, `RateLimiter`) means the gateway is **single-node by design**. The durable record is SQLite on `/data`; the worker registry is ephemeral and rebuilt lazily from persisted tokens after a restart.
- **The adapter owns identity normalization:** `LoginOk.account_key` is already normalized via `base.normalize_account_key` (strip + lowercase — the single owner of the rule); `oauth._finish` persists it as-is. Log event names and fields are a stable schema (operators query them in Railway logs) — refactors must not rename events or the `status`/`reason` values.
- **Path-scoped connectors:** each adapter is mounted under `/<adapter>` — the connector is `/<adapter>/mcp` (e.g. `/garmin/mcp`), OAuth endpoints are `/<adapter>/oauth/*`, and discovery is path-scoped: `/.well-known/oauth-authorization-server/<adapter>` (RFC 8414, issuer `PUBLIC_URL/<adapter>`) and `/.well-known/oauth-protected-resource/<adapter>/mcp` (RFC 9728). There is no bare `/mcp` alias.

## Testing approach

- `garminconnect` is **fully mocked** — the unit/integration suite never touches real Garmin. The worker manager and proxy are tested against a **fake worker HTTP server** (`tests/conftest.py::fake_worker`); the remote strategy against a **fake remote upstream** (`fake_remote`) driven through `conftest.StubRemoteAdapter` (`tests/test_remote_forward.py` + the generic authorize-flow tests in `test_oauth.py`); the local strategy the same way, through `conftest.StubLocalAdapter` (`tests/test_local_forward.py`); the upstream-OAuth login shape generically through `conftest.StubUpstreamOAuthAdapter` (`test_oauth.py`); backups against the same fake upstream posing as S3 (`tests/test_backup.py` — the SigV4 signer was additionally verified once against a real bucket). The whoop adapter itself (both pieces wired together) is covered end-to-end against a **fake WHOOP upstream** (`tests/conftest.py::fake_whoop`, a `FakeWhoopUpstream`) in `tests/test_whoop_e2e.py`.
- Consequently the **real `garminconnect` login/token-dump/resume path is not covered by automated tests**, and neither is the real WHOOP OAuth exchange/refresh. A manual end-to-end smoke test — Garmin (email/password, MFA) and WHOOP (provider sign-in, tool calls, and a token refresh once the access token expires) — is the release gate before connecting real users.
