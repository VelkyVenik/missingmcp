# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-user, OAuth 2.1–protected **remote MCP gateway** for connecting Garmin accounts to Claude (mobile/desktop/web). This fork ships only the Garmin adapter: it spawns and reverse-proxies to a per-user subprocess of the **unmodified** `garmin_mcp` worker (`github.com/Taxuspt/garmin_mcp`). The generic remote and local forwarding seams remain tested for future adapters, but no in-tree adapter currently uses them.

A rohlik adapter was implemented and then retired (2026-07-06, Rohlík ships its own OAuth MCP) — don't re-add it. Operator-facing docs (env-var reference, monitoring, deploy checklist) live in `README.md`; operational scripts (`status`, `revoke`, `usage`) live in `scripts/` and are documented in README → Monitoring.

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
  DATA_DIR=./.localdata uv run garmin

# Production (missingmcp.com) runs on Railway, built from the Dockerfile, and
# auto-deploys on every push to main — pushing = deploying. Verify after push:
# railway deployment list --json. (Self-host: plain `docker run` — see README.)
```

There is no separate lint step configured.

## Hard constraints

- **Never modify or import `garmin_mcp`.** Interact with it *only* as a black box via its documented CLI entrypoint (`garmin-mcp`) and env vars (`GARMIN_MCP_TRANSPORT`, `GARMIN_MCP_HOST`, `GARMIN_MCP_PORT`, `GARMINTOKENS`). No source edits, no importing its internal modules.
- **Pin `GARMIN_MCP_REF`** to a reviewed commit SHA in production (the `main` default is a floating ref — supply-chain risk). After bumping the pin, run `python scripts/gen_garmin_tools.py` — it regenerates the "All tools" section of `src/garmin/templates/garmin.html` from the new ref.
- **Python 3.12** (matches the worker's interpreter). All source under `src/garmin/`, all tests under `tests/`.

## Architecture

Request flow (one user, one device):

```
Claude → OAuth 2.1 (DCR → /<adapter>/oauth/register → /<adapter>/oauth/authorize → /<adapter>/oauth/token, PKCE S256, RFC 8414 discovery at /.well-known/oauth-authorization-server/<adapter>)
       → Garmin login (garminconnect; password discarded, tokens kept)
       → encrypted blob in SQLite, keyed by (adapter, account_key)
       → on POST /<adapter>/mcp, forward strategy (RFC 9728 discovery at /.well-known/oauth-protected-resource/<adapter>/mcp):
           worker (garmin): ensure the user's worker subprocess (127.0.0.1:<port>) → reverse-proxy
           remote (no in-tree adapter today): stream-forward to forward.upstream_url with forward.headers(blob) injected
           local (future adapters): forward.handle(conn, account_key, blob, body) runs an MCP server in-process
```

Modules (`src/garmin/`), in dependency order — one responsibility each, composing through small explicit contracts. **Full detail: [`docs/architecture.md`](docs/architecture.md).**

- **`config.py`** — `Config` frozen dataclass + `load_config(env)`; the single source of all tunables. Refuses to start without a valid `GATEWAY_SECRET`.
- **`log.py`** — structured JSON logging to stdout, with stdlib/uvicorn/warnings records bridged into the same stream.
- **`store.py`** — SQLite schema, AES-256-GCM crypto, token hashing, adapter-keyed CRUD, plus the data-hygiene ops the lifespan loop drives.
- **`security.py`** — PKCE S256 verify, redirect_uri allowlist, `CsrfStore`, sliding-window `RateLimiter`, security headers, `read_body_limited`.
- **`pages.py`** — `render_page`: wraps a `templates/` fragment in the shared site chrome, so every page is one visual site.
- **`adapters/base.py`** — the adapter contract: `Adapter` protocol, the three forward strategies as protocols, the login result shapes. The seam between the core and upstream services.
- **`adapters/garmin/`** — form-login adapter on the worker strategy: `garminconnect` wrapper, MFA state, worker CLI/env contract.
- **`oauth.py`** — metadata (RFC 8414), DCR (RFC 7591), the authorize form + adapter login + MFA two-step, and `/token` exchange.
- **`backup.py`** — off-box DB backups: SQLite snapshot → S3-compatible bucket, weekday-rotated keys. Disabled unless all `BACKUP_S3_*` are set.
- **`report.py`** — daily per-connector user-stats Slack report, computed from the DB. Disabled unless `SLACK_WEBHOOK_URL` is set. (Upstream also ships an hourly pager and a daily Claude-assisted triage job — `scripts/hourly_digest.py` / `scripts/daily_triage.py` — that read logs via the Railway GraphQL API; this self-hosted fork drops both, since they only work when deployed on Railway. See README → Monitoring.)
- **`usage.py`** — the public usage meter: per-adapter "N people used this in the last 30 days" count (30-day rolling window, protocol traffic excluded, hidden below 10), TTL-cached; `app.py` fills the `{USAGE_METER_<ADAPTER>}` placeholders per request.
- **`telemetry.py`** — PostHog events + an OTLP tee of the structured log stream. Env-gated by `POSTHOG_API_KEY` (unset ⇒ every function is a no-op), fire-and-forget throughout.
- **`workers.py`** — `WorkerManager`: per-account lazy spawn under a lock, `/healthz` poll, idle reaper, LRU cap, token-rotation read-back into the store; worker output pumped into the structured log.
- **`proxy.py`** — `authenticate` (Bearer + rate limits) and `handle_mcp`: shared core + strategy dispatch, every auth failure funnelling through `_reauth_required`.
- **`app.py`** — `build_app`: routes, middleware, shared singletons (one `WorkerManager` per worker-based adapter), the public `/subscribe` + `/suggest` endpoints, and the lifespan reap/cleanup loop. `main()` is the console entrypoint.

## Cross-cutting invariants (easy to break, hard to see from one file)

- **`account_key`** = the normalized **lowercased login email**, scoped by `adapter`. `(adapter, account_key)` is the join key across every table *and* (with `account_key` alone) the worker registry. A Bearer token carries its `adapter`; the proxy rejects a token used on a different adapter's `/mcp`.
- **Secret handling:** the Garmin **password is never persisted or logged** (held in a local, `del`-ed right after `start_login`). **Bearer tokens and client secrets are stored only as SHA-256 hashes.** Garmin tokens are AES-256-GCM encrypted at rest (`token files 0600`, `dirs 0700`). Logs carry at most an 8-char hash prefix. (A remote-strategy adapter may need to keep login credentials in its blob — the upstream authenticates every request — but they still live only inside the encrypted blob, never logged, never materialized to files.) **Telemetry egress obeys the same rule:** identity + metadata only, never MCP bodies, credentials or form contents — the account email travels only as PostHog's `distinct_id`.
- **Garmin rotates too — inside the worker's token file.** garth rewrites `garmin_tokens.json` on Garmin's refresh-token rotation, so `WorkerManager` persists that file back to the store (read-back via `forward.read_back`, injected `persist` callback): periodically from the lifespan loop (`persist_rotated()`, under the per-account lock), on reap/evict/shutdown, and on `ensure_worker`'s respawn path — which must materialize the recovered rotation, never the caller's stale blob. Never persist a torn (unparseable) file, and never "repair" from disk without a process-local baseline: a fresh process trusts the store (the file may predate a re-login) — drift older than the process is the explicit backfill's job.
- **Verify-then-persist:** in `oauth.py`, `adapter.verify` is the only "expectedly failing" step and gates `_finish` (which does upsert + code-mint + redirect) on **every** authorize path. A login/verify failure re-renders the form; a wrong MFA code re-prompts. Don't move `adapter.verify` back into `_finish`.
- **Blocking adapter sign-in runs off the event loop, capped.** `adapter.start_login` / `resume_second_factor` / `verify` do **synchronous** network I/O; every call site in async OAuth handlers must go through `_bounded` (`asyncio.to_thread` + `wait_for(config.login_timeout)`), never directly. A direct call freezes the single-node event loop for every user, and a rate-limited Garmin login can block for minutes. A timeout re-renders the form; the abandoned worker thread finishes on its own.
- **Per-IP rate limits assume a trusted reverse proxy.** Every `<name>:<ip>` limit keys on `request.client.host`, which is the real client IP only because `main()` runs uvicorn with `proxy_headers=True` + `forwarded_allow_ips` (default `*`) — on Railway the **leftmost** `X-Forwarded-For` is edge-controlled and non-spoofable. Remove that and all per-IP limits silently collapse into one shared-edge-IP bucket (cross-user DoS). `unauth:<ip>` gates only token-less requests; a valid Bearer is governed by `tok:<hash>` alone (the real per-session limit).
- **Worker reap/evict/replace all gate on `inflight`.** `reap_idle`, `_enforce_cap`, and `ensure_worker`'s reuse/replace path must never kill a worker with `inflight>0`; `ensure_worker` holds the worker in-flight across its `_healthy()` await (so a concurrent reap can't pop a just-validated worker), and `_enforce_cap` counts `_reserved` spawns toward the cap. **A terminated worker's port cools down** (skipped by `_alloc_port`, which round-robins instead of lowest-free-first) until its process is observed dead — a dying uvicorn still answers `/healthz`, and validating a fresh spawn against its predecessor's listener hands the forward a dead port (~100 user-facing 502s/day before the fix; reliability ticket 12). The proxy additionally retries a `ConnectError`ed worker forward once, re-running `ensure_worker`.
- **A healthy worker is not yet a signed-in worker.** Since garmin_mcp `e8554bc` the worker logs in to Garmin on a background thread and answers `/healthz` *before* the sign-in resolves, so `ensure_worker` additionally blocks on the **login gate** (a per-spawn `LoginGate` the output pump fills from the worker's sign-in log lines via `forward.login_outcome` — optional on the WorkerForward protocol, probed with `getattr` like `read_back`), inside the same `worker_startup_timeout` budget: a "failed" line → `WorkerCredentialsRejected` → re-auth 401 (self-heal, event `worker-login-rejected`), silence → plain `WorkerStartError` (event `worker-login-timeout`). Without the gate, stale tokens would surface as per-call "run garmin-mcp-auth" tool errors a garmin user can't act on, and the re-auth flow would never trigger. Injected test spawns carry no gate and skip the wait.
- **PKCE S256 only** (`plain` rejected); **`redirect_uri` exact-match allowlist** enforced on `authorize_get`, the login branch *and* the MFA branch of `authorize_post`, and at `/token`.
- **Workers bind `127.0.0.1` only** — only the gateway reaches them. TLS terminates in front of the gateway (the Railway edge in production; a self-hoster brings their own proxy).
- **Process-local state** (worker registry, `AuthState`, `CsrfStore`, `RateLimiter`) means the gateway is **single-node by design**. The durable record is SQLite on `/data`; the worker registry is ephemeral and rebuilt lazily from persisted tokens after a restart.
- **The adapter owns identity normalization:** `LoginOk.account_key` is already normalized via `base.normalize_account_key` (strip + lowercase — the single owner of the rule); `oauth._finish` persists it as-is.
- **Everything goes to stdout as structured JSON.** NOTHING may write plain text to stderr — Railway classifies it as error-severity. Log event names and fields are a stable schema (operators query them in Railway logs) — refactors must not rename events or the `status`/`reason` values.
- **Path-scoped connectors:** each adapter is mounted under `/<adapter>` — the connector is `/<adapter>/mcp` (e.g. `/garmin/mcp`), OAuth endpoints are `/<adapter>/oauth/*`, and discovery is path-scoped: `/.well-known/oauth-authorization-server/<adapter>` (RFC 8414, issuer `PUBLIC_URL/<adapter>`) and `/.well-known/oauth-protected-resource/<adapter>/mcp` (RFC 9728). There is no bare `/mcp` alias.
- **Adapter retirement is an explicit list, never registry-absence.** `adapters.RETIRED_ADAPTERS` names adapters whose rows the cleanup loop purges. Registry absence alone must never trigger data deletion ([ADR-0001](docs/adr/0001-retired-adapter-cleanup.md)).

## Testing approach

- `garminconnect` is **fully mocked** — the unit/integration suite never touches real Garmin. Worker, remote, local, upstream-OAuth, and backup seams use fake upstreams.
- Consequently the **real `garminconnect` login/token-dump/resume path is not covered by automated tests**. A manual Garmin smoke test (email/password and MFA) is the release gate before connecting real users.

## Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root.
