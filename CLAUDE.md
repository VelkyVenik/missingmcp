# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-user, OAuth 2.1–protected **remote MCP gateway** that lets a small trusted circle each connect their own Garmin account to Claude (mobile/desktop/web). It wraps the **unmodified** `garmin_mcp` worker (`github.com/Taxuspt/garmin_mcp`): the gateway terminates OAuth, performs the Garmin login, stores per-account encrypted tokens, and for each account spawns + reverse-proxies to a per-user `garmin-mcp` subprocess.

The canonical design and the task-by-task implementation plan live in `docs/superpowers/specs/` and `docs/superpowers/plans/` — read them for rationale and the full data flow. Operator-facing docs (env-var reference, monitoring, deploy checklist) live in `README.md`; operational scripts (`status`, `monitor`, `revoke`, `usage`, `health`) live in `scripts/` and are documented in README → Monitoring.

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
# To exercise the full /mcp path locally, also set GARMIN_MCP_CMD (garmin-mcp isn't on
# PATH): GARMIN_MCP_CMD="uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp"
GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 \
  DATA_DIR=./.localdata uv run garmin-gateway

# Full deployment (installs the pinned garmin-mcp worker, spawns per-user workers).
cp .env.example .env   # set a real GATEWAY_SECRET, PUBLIC_URL, and pin GARMIN_MCP_REF to a commit SHA
docker compose up -d --build
```

There is no separate lint step configured.

## Hard constraints

- **Never modify or import `garmin_mcp`.** Interact with it *only* as a black box via its documented CLI entrypoint (`garmin-mcp`) and env vars (`GARMIN_MCP_TRANSPORT`, `GARMIN_MCP_HOST`, `GARMIN_MCP_PORT`, `GARMINTOKENS`). No source edits, no importing its internal modules.
- **Pin `GARMIN_MCP_REF`** to a reviewed commit SHA in production (the `main` default is a floating ref — supply-chain risk).
- **Python 3.12** (matches the worker's interpreter). All source under `src/garmin_gateway/`, all tests under `tests/`.

## Architecture

Request flow (one user, one device):

```
Claude → OAuth 2.1 (DCR → /authorize → /token, PKCE S256)
       → Garmin web login via garminconnect (password discarded, tokens kept)
       → encrypted tokens in SQLite, keyed by garmin_user_key
       → on POST /mcp: ensure the user's garmin-mcp subprocess (127.0.0.1:<port>) → reverse-proxy
```

Modules (`src/garmin_gateway/`), in dependency order — each has one responsibility and composes through small explicit contracts:

- **`config.py`** — `Config` frozen dataclass + `load_config(env)`. Single source of all tunables (read from env). Refuses to start without a valid `GATEWAY_SECRET`.
- **`log.py`** — structured JSON logging to stdout (`log` / `log_warn` / `log_error`). Callers must never pass secrets; the runtime (Docker/journald) supplies timestamps.
- **`store.py`** — SQLite schema + AES-256-GCM crypto + token hashing + CRUD. Five tables: `garmin_accounts` (encrypted tokens), `access_tokens` (Bearer hash → account), `oauth_clients` (DCR), `oauth_codes` (one-time PKCE codes), `tool_usage` (per-account tool metrics). Encryption key = `SHA-256(GATEWAY_SECRET)`.
- **`security.py`** — PKCE S256 verify, redirect_uri allowlist, `CsrfStore`, sliding-window `RateLimiter`, security headers, `read_body_limited`.
- **`garmin_login.py`** — thin `garminconnect` wrapper: `start_login` (MFA-aware, `return_on_mfa=True`; retries transient Garmin/Cloudflare blocks with a short backoff — a bad password is never retried), `resume_login`, `verify_tokens`. Token capture mirrors `garmin_mcp/auth_cli.py` (dump via garth client → read `garmin_tokens.json`).
- **`oauth.py`** — one cohesive module covering metadata (RFC 8414), DCR (RFC 7591), the `/authorize` form + Garmin login + MFA two-step, and `/token` exchange. `AuthState` holds the in-memory MFA-pending map (TTL 300s).
- **`workers.py`** — `WorkerManager`: per-account `asyncio.Lock` (no double-spawn), lazy spawn, `/healthz` poll, idle reaper, LRU cap, and token materialization to `0700` dirs / `0600` files. `spawn` is injectable for tests.
- **`proxy.py`** — `authenticate` (Bearer + rate limits) and `handle_mcp` (body-limit → `ensure_worker` → stream-forward to the worker, mapping timeout→504, start-failure→502).
- **`app.py`** — `build_app(config)` wires routes + security-headers middleware + shared singletons (db conn, `WorkerManager`, `AuthState`, `RateLimiter`) + a lifespan that periodically reaps idle workers and cleans expired codes. `main()` is the `garmin-gateway` console entrypoint.

## Cross-cutting invariants (easy to break, hard to see from one file)

- **`garmin_user_key`** = the normalized **lowercased Garmin login email**. It is the join key across every table *and* the worker registry — so a user's phone and desktop share one warm worker.
- **Secret handling:** the Garmin **password is never persisted or logged** (held in a local, `del`-ed right after `start_login`). **Bearer tokens and client secrets are stored only as SHA-256 hashes.** Garmin tokens are AES-256-GCM encrypted at rest (`token files 0600`, `dirs 0700`). Logs carry at most an 8-char hash prefix.
- **Verify-then-persist:** in `oauth.py`, `verify_tokens` is the only "expectedly failing" step and gates `_finish` (which does upsert + code-mint + redirect) on **every** authorize path. A login/verify failure re-renders the form; a wrong MFA code re-prompts. Don't move `verify_tokens` back into `_finish`.
- **PKCE S256 only** (`plain` rejected); **`redirect_uri` exact-match allowlist** enforced on `authorize_get`, the login branch *and* the MFA branch of `authorize_post`, and at `/token`.
- **Workers bind `127.0.0.1` only**; the compose file publishes `127.0.0.1:8080:8080`. Only the gateway reaches workers; nginx (operator-managed) terminates TLS in front.
- **Process-local state** (worker registry, `AuthState`, `CsrfStore`, `RateLimiter`) means the gateway is **single-node by design**. The durable record is SQLite on `/data`; the worker registry is ephemeral and rebuilt lazily from persisted tokens after a restart.

## Testing approach

- `garminconnect` is **fully mocked** — the unit/integration suite never touches real Garmin. The worker manager and proxy are tested against a **fake worker HTTP server** (`tests/conftest.py::fake_worker`).
- Consequently the **real `garminconnect` login/token-dump/resume path is not covered by automated tests**. A manual end-to-end smoke test with a real Garmin account (including the MFA path) is the release gate before connecting real users.
