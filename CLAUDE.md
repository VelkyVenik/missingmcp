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

# After changing the link-preview card's copy or palette, redraw static/og.png
# (Pillow is not a project dependency, hence --with). The `?v=` cache-buster in
# pages.py is derived from the file's hash, so a redraw invalidates scraper caches
# by itself:
uv run --with pillow python scripts/gen_og_image.py

# Production (missingmcp.com) runs on Railway, built from the Dockerfile, and
# auto-deploys on every push to main — pushing = deploying. Verify after push:
# railway deployment list --json. (Self-host: plain `docker run` — see README.)
```

There is no separate lint step configured.

## Hard constraints

- **Never modify or import `garmin_mcp`.** Interact with it *only* as a black box via its documented CLI entrypoint (`garmin-mcp`) and env vars (`GARMIN_MCP_TRANSPORT`, `GARMIN_MCP_HOST`, `GARMIN_MCP_PORT`, `GARMINTOKENS`). No source edits, no importing its internal modules.
- **Pin `GARMIN_MCP_REF`** to a reviewed commit SHA in production (the `main` default is a floating ref — supply-chain risk). After bumping the pin, run `python scripts/gen_garmin_tools.py` — it regenerates the "All tools" section of `src/missingmcp/templates/garmin.html` from the new ref.
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

Modules (`src/missingmcp/`), in dependency order — one responsibility each, composing through small explicit contracts. **Full detail: [`docs/architecture.md`](docs/architecture.md).**

- **`config.py`** — `Config` frozen dataclass + `load_config(env)`; the single source of all tunables. Refuses to start without a valid `GATEWAY_SECRET`.
- **`log.py`** — structured JSON logging to stdout, with stdlib/uvicorn/warnings records bridged into the same stream.
- **`store.py`** — SQLite schema, AES-256-GCM crypto, token hashing, adapter-keyed CRUD, plus the data-hygiene ops the lifespan loop drives.
- **`security.py`** — PKCE S256 verify, redirect_uri allowlist, `CsrfStore`, sliding-window `RateLimiter`, security headers, `read_body_limited`.
- **`pages.py`** — `render_page`: wraps a `templates/` fragment in the shared site chrome, so every page is one visual site.
- **`adapters/base.py`** — the adapter contract: `Adapter` protocol, the three forward strategies as protocols, the login result shapes. The seam between the core and upstream services.
- **`adapters/garmin/`** — form-login adapter on the worker strategy: `garminconnect` wrapper, MFA state, worker CLI/env contract.
- **`adapters/whoop/`** — upstream-OAuth adapter on the local strategy: `api.py` (WHOOP v2 client + rotating refresh), `mcp.py` (the in-process MCP server that *is* `/whoop/mcp`).
- **`oauth.py`** — metadata (RFC 8414), DCR (RFC 7591), the authorize form + adapter login + MFA two-step, and `/token` exchange.
- **`backup.py`** — off-box DB backups: SQLite snapshot → S3-compatible bucket, weekday-rotated keys. Disabled unless all `BACKUP_S3_*` are set.
- **`report.py`** — daily per-connector user-stats Slack report, computed from the DB. Disabled unless `SLACK_WEBHOOK_URL` is set. (Two more reports are separate **external** jobs, because the app can't read its own stdout logs: the *hourly hard-signal pager* — `scripts/hourly_digest.py` + `.github/workflows/hourly-digest.yml`, pages only on site-down/worker-fault-burst/5xx/critical — and the *daily triage* — `scripts/daily_triage.py` + `.github/workflows/daily-triage.yml`, Railway-log aggregates + Claude analysis with proposed actions (subscription-backed via `claude -p`, API key as fallback). All operator posting in `daily_triage.py` goes through its `notify()` seam — the channel may move off Slack.)
- **`usage.py`** — the public usage meter: per-adapter "N people used this in the last 30 days" count (30-day rolling window, protocol traffic excluded, hidden below 10), TTL-cached; `app.py` fills the `{USAGE_METER_<ADAPTER>}` placeholders per request.
- **`telemetry.py`** — PostHog events + an OTLP tee of the structured log stream. Env-gated by `POSTHOG_API_KEY` (unset ⇒ every function is a no-op), fire-and-forget throughout.
- **`workers.py`** — `WorkerManager`: per-account lazy spawn under a lock, `/healthz` poll, idle reaper, LRU cap, token-rotation read-back into the store; worker output pumped into the structured log.
- **`proxy.py`** — `authenticate` (Bearer + rate limits) and `handle_mcp`: shared core + strategy dispatch, every auth failure funnelling through `_reauth_required`.
- **`app.py`** — `build_app`: routes, middleware, shared singletons (one `WorkerManager` per worker-based adapter), the public `/subscribe` + `/suggest` endpoints, and the lifespan reap/cleanup loop. `main()` is the console entrypoint.

## Cross-cutting invariants (easy to break, hard to see from one file)

- **`account_key`** = the normalized **lowercased login email**, scoped by `adapter`. `(adapter, account_key)` is the join key across every table *and* (with `account_key` alone) the worker registry. A Bearer token carries its `adapter`; the proxy rejects a token used on a different adapter's `/mcp`.
- **Secret handling:** the Garmin **password is never persisted or logged** (held in a local, `del`-ed right after `start_login`). **Bearer tokens and client secrets are stored only as SHA-256 hashes.** Garmin tokens are AES-256-GCM encrypted at rest (`token files 0600`, `dirs 0700`). Logs carry at most an 8-char hash prefix. (A remote-strategy adapter may need to keep login credentials in its blob — the upstream authenticates every request — but they still live only inside the encrypted blob, never logged, never materialized to files.) **Telemetry egress obeys the same rule:** identity + metadata only, never MCP bodies, credentials or form contents — the account email travels only as PostHog's `distinct_id`.
- **WHOOP refresh tokens rotate on every use:** WHOOP invalidates the old refresh token the instant a new one is issued, so only the gateway ever refreshes (never a worker — there is no whoop worker), refreshes are serialized per account under an `asyncio.Lock` (`WhoopApi.ensure_fresh`), and the rotated blob is always persisted to the store *before* it's used for a request. Refresh requests carry `scope: offline` — the scope WHOOP requires to keep issuing refresh tokens at all.
- **Garmin rotates too — inside the worker's token file.** garth rewrites `garmin_tokens.json` on Garmin's refresh-token rotation, so `WorkerManager` persists that file back to the store (read-back via `forward.read_back`, injected `persist` callback): periodically from the lifespan loop (`persist_rotated()`, under the per-account lock), on reap/evict/shutdown, and on `ensure_worker`'s respawn path — which must materialize the recovered rotation, never the caller's stale blob. Never persist a torn (unparseable) file, and never "repair" from disk without a process-local baseline: a fresh process trusts the store (the file may predate a re-login) — drift older than the process is the explicit backfill's job.
- **Verify-then-persist:** in `oauth.py`, `adapter.verify` is the only "expectedly failing" step and gates `_finish` (which does upsert + code-mint + redirect) on **every** authorize path. A login/verify failure re-renders the form; a wrong MFA code re-prompts. Don't move `adapter.verify` back into `_finish`.
- **Blocking adapter sign-in runs off the event loop, capped.** `adapter.start_login` / `resume_second_factor` / `verify` do **synchronous** network I/O (garminconnect, WHOOP profile fetch); **every** call site in the async OAuth handlers — `oauth.authorize_post` (login + MFA) **and** `oauth.authorize_callback` (upstream-OAuth verify) — must go through `_bounded` (`asyncio.to_thread` + `wait_for(config.login_timeout)`), never directly. A direct call freezes the single-node event loop for every user, and a Garmin login that's being rate-limited can block ~2 minutes (a 125s POST was observed; the callback `verify` was a later-caught instance of the same bug). A timeout re-renders the form (`*-timeout` log events); the abandoned worker thread finishes on its own.
- **Per-IP rate limits assume a trusted reverse proxy.** Every `<name>:<ip>` limit keys on `request.client.host`, which is the real client IP only because `main()` runs uvicorn with `proxy_headers=True` + `forwarded_allow_ips` (default `*`) — on Railway the **leftmost** `X-Forwarded-For` is edge-controlled and non-spoofable. Remove that and all per-IP limits silently collapse into one shared-edge-IP bucket (cross-user DoS). `unauth:<ip>` gates only token-less requests; a valid Bearer is governed by `tok:<hash>` alone (the real per-session limit).
- **Worker reap/evict/replace all gate on `inflight`.** `reap_idle`, `_enforce_cap`, and `ensure_worker`'s reuse/replace path must never kill a worker with `inflight>0`; `ensure_worker` holds the worker in-flight across its `_healthy()` await (so a concurrent reap can't pop a just-validated worker), and `_enforce_cap` counts `_reserved` spawns toward the cap. **A terminated worker's port cools down** (skipped by `_alloc_port`, which round-robins instead of lowest-free-first) until its process is observed dead — a dying uvicorn still answers `/healthz`, and validating a fresh spawn against its predecessor's listener hands the forward a dead port (~100 user-facing 502s/day before the fix; reliability ticket 12). The proxy additionally retries a `ConnectError`ed worker forward once, re-running `ensure_worker`.
- **PKCE S256 only** (`plain` rejected); **`redirect_uri` exact-match allowlist** enforced on `authorize_get`, the login branch *and* the MFA branch of `authorize_post`, and at `/token`.
- **Workers bind `127.0.0.1` only** — only the gateway reaches them. TLS terminates in front of the gateway (the Railway edge in production; a self-hoster brings their own proxy).
- **Process-local state** (worker registry, `AuthState`, `CsrfStore`, `RateLimiter`) means the gateway is **single-node by design**. The durable record is SQLite on `/data`; the worker registry is ephemeral and rebuilt lazily from persisted tokens after a restart.
- **The adapter owns identity normalization:** `LoginOk.account_key` is already normalized via `base.normalize_account_key` (strip + lowercase — the single owner of the rule); `oauth._finish` persists it as-is.
- **Everything goes to stdout as structured JSON.** NOTHING may write plain text to stderr — Railway classifies it as error-severity. Log event names and fields are a stable schema (operators query them in Railway logs) — refactors must not rename events or the `status`/`reason` values.
- **Path-scoped connectors:** each adapter is mounted under `/<adapter>` — the connector is `/<adapter>/mcp` (e.g. `/garmin/mcp`), OAuth endpoints are `/<adapter>/oauth/*`, and discovery is path-scoped: `/.well-known/oauth-authorization-server/<adapter>` (RFC 8414, issuer `PUBLIC_URL/<adapter>`) and `/.well-known/oauth-protected-resource/<adapter>/mcp` (RFC 9728). There is no bare `/mcp` alias.
- **Adapter retirement is an explicit list, never registry-absence.** `adapters.RETIRED_ADAPTERS` names the adapters whose rows the cleanup loop purges from every table. A missing env var (e.g. `WHOOP_*`) drops an adapter from the registry *without* retiring it — conflating the two deletes live users' data ([ADR-0001](docs/adr/0001-retired-adapter-cleanup.md)).

## Testing approach

- `garminconnect` is **fully mocked** — the unit/integration suite never touches real Garmin. The worker manager and proxy are tested against a **fake worker HTTP server** (`tests/conftest.py::fake_worker`); the remote strategy against a **fake remote upstream** (`fake_remote`) driven through `conftest.StubRemoteAdapter` (`tests/test_remote_forward.py` + the generic authorize-flow tests in `test_oauth.py`); the local strategy the same way, through `conftest.StubLocalAdapter` (`tests/test_local_forward.py`); the upstream-OAuth login shape generically through `conftest.StubUpstreamOAuthAdapter` (`test_oauth.py`); backups against the same fake upstream posing as S3 (`tests/test_backup.py` — the SigV4 signer was additionally verified once against a real bucket). The whoop adapter itself (both pieces wired together) is covered end-to-end against a **fake WHOOP upstream** (`tests/conftest.py::fake_whoop`, a `FakeWhoopUpstream`) in `tests/test_whoop_e2e.py`.
- Consequently the **real `garminconnect` login/token-dump/resume path is not covered by automated tests**, and neither is the real WHOOP OAuth exchange/refresh. A manual end-to-end smoke test — Garmin (email/password, MFA) and WHOOP (provider sign-in, tool calls, and a token refresh once the access token expires) — is the release gate before connecting real users.

## Agent skills

### Issue tracker

Issues and PRDs live as markdown files under `.scratch/<feature>/` in this repo (no remote issue tracker). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary — each triage role's string equals its name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), recorded as a `Status:` line in each issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
