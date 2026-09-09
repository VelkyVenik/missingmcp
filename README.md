# Garmin

*The MCP servers exist. Connecting them shouldn't be complicated.*

[![smithery badge](https://smithery.ai/badge/vaclav/garmin)](https://smithery.ai/servers/vaclav/garmin)

A multi-user, OAuth 2.1–protected gateway that lets a small trusted circle
connect their Garmin accounts from any Claude client (iOS, Android, Web,
Desktop). For [Garmin Connect](https://connect.garmin.com), the gateway wraps
the **unmodified** [`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) worker and
adds OAuth, per-user token isolation, and a reverse proxy. The core remains
adapter-based, but this fork ships only the Garmin worker adapter. The
reference deployment runs at
[missingmcp.com](https://missingmcp.com).

```
Claude → POST /garmin/mcp (Bearer) → Gateway → 127.0.0.1:<port>/mcp (per-user garmin_mcp) → connect.garmin.com
```

## Why

`garmin_mcp` is a great MCP server, but it's single-user and stdio-only: each
person has to run it locally with their own Garmin tokens. This gateway makes it a
**remote MCP server** any Claude client can connect to over HTTP, with a proper
OAuth sign-in flow — so non-technical users just click "connect" and log in with
their Garmin credentials, and never touch a terminal or a token file.

## Features

- **OAuth 2.1** — Authorization Code + PKCE (S256) with Dynamic Client
  Registration. Connect from any Claude client; no manual token wrangling.
- **Garmin password is never stored** — used once to sign in (MFA supported);
  only the resulting session tokens are persisted.
- **Encrypted at rest** — tokens sealed with AES-256-GCM; the DB is useless without
  `GATEWAY_SECRET`. Bearer tokens are stored only as SHA-256 hashes.
- **Per-user isolation** — each account gets its own `garmin_mcp` worker bound to
  `127.0.0.1`, started on demand and reaped when idle.
- **Hardened** — one-time 10-min auth codes, CSRF on forms, per-IP/-token rate
  limits, the `garmin_mcp` worker pinned to a reviewed commit.
- **Off-box backups** — periodic encrypted SQLite snapshots to an S3-compatible
  bucket (see [Backups](#backups)).
- **Instructional landing page** served on `/` and as a friendly fallback for
  unknown paths.

## Deploy

**Railway (how missingmcp.com runs):** one service built from the `Dockerfile`,
a volume mounted at `/data`, a single replica (the gateway keeps process-local
state by design), and TLS terminated at the Railway edge. Set the env vars from
[Configuration](#configuration) on the service; pushes to `main` deploy
automatically once the service is connected to the GitHub repo. An optional
Railway bucket enables [Backups](#backups) (`railway bucket create backups`,
then wire its credentials as the `BACKUP_S3_*` variables).

**Self-hosted (plain Docker):**

```bash
cp .env.example .env          # set GATEWAY_SECRET, PUBLIC_URL, pin GARMIN_MCP_REF
docker build -t garmin .
docker run -d --name garmin --restart unless-stopped --env-file .env \
  -p 127.0.0.1:8080:8080 -v garmin-data:/data garmin
```

Or with the included `docker-compose.yml` (same image, same volume, does the
build for you — handy for a plain droplet running Docker Compose):

```bash
cp .env.example .env          # set GATEWAY_SECRET, PUBLIC_URL, pin GARMIN_MCP_REF
docker compose up -d --build
docker compose logs -f        # live structured-JSON events
```

Put any TLS-terminating proxy in front (Caddy, nginx, Traefik). One non-obvious
requirement: `/​<adapter>/mcp` streams SSE, so disable response buffering and
raise the read timeout (nginx: `proxy_buffering off; proxy_read_timeout 3600s;`).
Then add `https://<your-domain>/garmin/mcp` as a remote MCP server in Claude.

## Local development

```bash
uv pip install -e ".[dev]"
uv run --extra dev pytest -q                 # run the test suite

# Run the gateway locally (no Garmin account needed to exercise the OAuth surface).
# garmin-mcp isn't on PATH locally, so point GARMIN_MCP_CMD at uvx.
GATEWAY_SECRET="$(openssl rand -base64 48)" \
PUBLIC_URL=http://localhost:8088 PORT=8088 DATA_DIR=./.localdata \
GARMIN_MCP_CMD="uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp" \
  uv run garmin
```

A `.env` file in the working directory is loaded automatically (real environment
variables take precedence), so you can drop the same values there instead.

## Connectors

Each connector is mounted under its own path prefix with its own OAuth flow and
its own Bearer tokens (there is no bare `/mcp`). The landing page on `/` lists
the available connectors.

### Garmin — `/garmin/mcp`

Sign in with your Garmin Connect email + password (MFA supported). The password
is used once for the Garmin login and discarded; only the resulting Garmin
session tokens are stored (AES-256-GCM encrypted). Each account gets its own
`garmin_mcp` worker process, bound to `127.0.0.1`, started on demand and reaped
when idle.

### Rohlík — no longer missing

The gateway briefly served a Rohlík connector (remote strategy: credential
headers injected into the hosted Rohlík MCP). It was retired in 2026-07 when
Rohlík shipped **its own OAuth-protected MCP** — add
`https://mcp.rohlik.cz/mcp` directly as a custom connector in Claude and sign
in in the browser ([Rohlík's guide](https://www.rohlik.cz/stranka/mcp-server)).
The remote-forward strategy remains a first-class, tested part of the core
(`tests/test_remote_forward.py`) for the next service that needs it.

## Connecting from Claude

1. In any Claude client: **Settings → Connectors → Add custom connector**, or in
   the CLI: `claude mcp add --transport http garmin https://<your-domain>/garmin/mcp`.
2. Claude opens the gateway's sign-in page — enter your Garmin email +
   password (and an MFA code when needed).
3. Done — the service's tools are now available in Claude.

## Configuration

Set via environment (or `.env`). See [`.env.example`](.env.example).

| Variable | Required | Default | Description |
|---|---|---|---|
| `GATEWAY_SECRET` | **yes** | — | ≥32-char key for token encryption. Refuses to start with the placeholder. Generate with `openssl rand -base64 48`. |
| `PUBLIC_URL` | yes | `http://localhost:8080` | Public URL used in OAuth metadata + redirects. |
| `PORT` | no | `8080` | Listen port. |
| `DATA_DIR` | no | `/data` | Where the SQLite DB and per-user token dirs live. |
| `DB_PATH` | no | `$DATA_DIR/gateway.db` | Override the DB path. |
| `GARMIN_MCP_CMD` | no | `garmin-mcp` | Command to spawn the worker. Use a `uvx …` invocation when `garmin-mcp` isn't on PATH. |
| `GARMIN_MCP_REF` | no | pinned SHA in `Dockerfile` | Docker build arg: commit of `garmin_mcp` to install. Bumping it is a deliberate, reviewed action — afterwards run `python scripts/gen_garmin_tools.py` to refresh the tool listing on the `/garmin` page. |
| `WORKER_PORT_START` / `WORKER_PORT_END` | no | `9000` / `9099` | Port range for per-user workers. |
| `WORKER_IDLE_TTL` | no | `900` | Seconds before an idle worker is reaped. |
| `WORKER_STARTUP_TIMEOUT` | no | `20` | Seconds to wait for a worker to become healthy. |
| `MAX_WORKERS` | no | `10` | Max concurrent per-user workers. |
| `ACCESS_TOKEN_TTL_DAYS` | no | `90` | Bearer token lifetime; user re-authenticates after it. `0` disables expiry. |
| `OPERATOR_NAME` / `OPERATOR_EMAIL` | no | — | Shown on the landing page. |
| `OPERATOR_URL` | no | — | Homepage the operator name links to (footer, trust notes). Unset → plain text. |
| `BACKUP_S3_ENDPOINT` / `BACKUP_S3_BUCKET` / `BACKUP_S3_ACCESS_KEY` / `BACKUP_S3_SECRET_KEY` | no | — | S3-compatible bucket for off-box DB backups (see Backups). Backups are disabled unless all four are set. |
| `BACKUP_S3_REGION` | no | `auto` | SigV4 region of the bucket. |
| `BACKUP_S3_URL_STYLE` | no | `virtual-host` | `virtual-host` (Railway buckets) or `path`. |
| `BACKUP_INTERVAL_HOURS` | no | `6` | Hours between backup uploads. First backup runs right after startup. |
| `POSTHOG_API_KEY` | no | — | Public `phc_` project key. Unset ⇒ **all** PostHog telemetry (events, log tee, web analytics) is off. |
| `POSTHOG_HOST` | no | `https://eu.i.posthog.com` | Server-side ingestion host (SDK events + OTLP log tee). |
| `POSTHOG_WEB_HOST` | no | `$POSTHOG_HOST` | posthog-js `api_host` — set it to the managed reverse proxy (e.g. `https://j.missingmcp.com`) for ad-blocker resilience. |
| `POSTHOG_UI_HOST` | no | `https://eu.posthog.com` | PostHog app host; posthog-js needs it when `POSTHOG_WEB_HOST` is a proxy. |
| `GATEWAY_LOG_FILE` | no | — | If set, tees structured + stdlib logs to this file. |
| `GATEWAY_LOG_LEVEL` | no | `info` | `debug`\|`info`\|`warning`\|`error`\|`critical`. `debug` is verbose (logs garminconnect/urllib3 internals) — avoid in production. |

## Backups

When the `BACKUP_S3_*` variables are set, the gateway uploads a consistent
SQLite snapshot (via the SQLite backup API, WAL-safe) to the bucket right
after startup and then every `BACKUP_INTERVAL_HOURS`. Keys rotate by weekday
(`db/gateway-mon.db` … `db/gateway-sun.db`), giving seven days of retention
with no cleanup logic. Watch the `backup-ok` / `backup-failed` log events.

Only the DB is backed up — per-user token dirs under `DATA_DIR/users/` are
re-materialized from the DB on demand.

> **The backup is useless without `GATEWAY_SECRET`** (account blobs stay
> AES-256-GCM encrypted inside it) — and that is the point. Keep a copy of
> `GATEWAY_SECRET` somewhere that is *not* the bucket and *not* Railway
> (e.g. your password manager). Losing the secret = losing every account.

**Restore:** download the newest object, put it at `$DATA_DIR/gateway.db`
(delete any stale `gateway.db-wal`/`-shm` next to it), set the same
`GATEWAY_SECRET`, start the gateway. Bearer tokens and logins survive;
workers respawn lazily.

## Monitoring

Three helper scripts work directly on the gateway's DB (safe to run while the gateway is live):

```bash
python scripts/status.py          # summary counts only (safe to paste/share)
python scripts/status.py --detail # + per-account devices (token prefixes),
                                  #   usage summary, OAuth clients, running workers
python scripts/revoke.py --list                       # accounts + token counts
python scripts/revoke.py --account [<adapter>:]<key>  # kill-switch: revoke ALL the
                                                      #   account's tokens (bare key = garmin)
python scripts/revoke.py --account <key> --purge      # + delete stored account & usage
python scripts/revoke.py --device <hash-prefix>       # revoke ONE device (prefix from status.py)
python scripts/usage.py                               # per-account tool usage + leaderboard
python scripts/usage.py --account [<adapter>:]<key>   # one account's per-tool breakdown
python scripts/subscribers.py                         # newsletter signups + suggestions
python scripts/subscribers.py --emails                # subscriber emails, one per line
python scripts/add_beer.py --email <supporter>        # record a "buy me a beer" donation (1 beer, 5 EUR)
python scripts/add_beer.py --email <supporter> --beers 3 --at 2026-07-20  # 3 beers, backdated
```

The gateway also posts a daily user-stats report to Slack on its own each
morning (`DAILY_REPORT_HOUR`, default 08:00 `DAILY_REPORT_TZ`) when
`SLACK_WEBHOOK_URL` is set (`src/garmin/report.py`).

> This fork drops the upstream project's `scripts/hourly_digest.py` /
> `scripts/daily_triage.py` (and their GitHub Actions workflows) — both read
> logs through the Railway GraphQL API, so they only work when deployed on
> Railway. A self-hoster on their own box needs a different log pipeline for
> that kind of alerting; `docker logs` / your platform's own log shipping is
> the starting point (see the structured-JSON logging note below).

**PostHog telemetry** — with `POSTHOG_API_KEY` set (see the env table), the
gateway also ships analytics to PostHog (EU cloud):
per-request `$mcp_*` events (PostHog's built-in MCP analytics), a lean
connect-funnel/conversion event set, an OTLP tee of the structured log stream
(PostHog Logs, beta — Railway stays the durable archive), and posthog-js on the
site (UTM campaign attribution; autocapture disabled on OAuth pages). Egress
rule: identity + metadata only — never MCP bodies, credentials, or form
contents; the account email travels only as `distinct_id`. Everything is
fire-and-forget: a PostHog outage never blocks a request. The daily Slack
report above keeps running unchanged — PostHog complements it. When
off-boarding a user (`revoke.py --purge`), also delete the person in PostHog
(People → delete) to complete the GDPR path.

**Beer supporters** — `scripts/add_beer.py` records a "buy me a beer" donation
(`buymeacoffee.com/venik`) into a local `beers` audit table and emits a
`beer_purchased` PostHog event, best-effort attributed to the supporter's
gateway account (`matched` when the email is a known login). Ingestion is
manual (run the script by hand per donation). Same egress rule as above — the
email travels only as `distinct_id`, never the supporter name or note.

**With Docker** the scripts are baked into the image at `/app/scripts`; run them
inside the container. `status.py` finds the DB under `/data` automatically:

```bash
docker exec garmin python /app/scripts/status.py
docker logs -f garmin                   # live structured-JSON events
```

**On Railway** run them over `railway ssh`; logs live in the Railway dashboard
(`railway logs --service gateway` for a live tail):

```bash
railway ssh --service gateway "python3 /app/scripts/status.py"
railway ssh --service gateway "python3 /app/scripts/revoke.py --account <email>"
```

All logging is structured JSON on stdout (one event per line) with a proper
`level` attribute — including uvicorn/stdlib records (event `stdlib-log`) and
each worker's own output (event `worker-log`, with an `account` attribute;
lines matching ERROR/Traceback are elevated to error severity). On Railway
that makes everything searchable in the log explorer — filter e.g.
`@event:worker-log @account:<email>` to trace one user's worker, or
`@level:error` for problems. Request latency is recorded per call in the
`mcp-response` event (`ttfb_ms`, `total_ms`, `bytes`, `tool`, `account`);
login/verify and worker-spawn events carry `ms` durations. The gateway also
logs a `stats` event (accounts / tokens / people-with-token / clients /
active-workers) on startup and whenever those counts change, and `status.py`
lists the running workers.

### Automatic data hygiene

The gateway keeps its own DB tidy — no manual grooming needed. Alongside expiring
old auth codes and access tokens, the background loop (roughly once a minute):

- **sweeps abandoned OAuth clients** — Claude registers a fresh client (DCR) on
  every connection attempt, and one that never completes the flow leaves a
  token-less client behind. Any client with **zero access tokens** older than one
  hour (comfortably above the 10-min code lifetime, so an in-progress sign-in is
  never touched) is removed. This is why `status.py`'s OAuth-client count tracks
  the number of connected devices rather than growing with every failed attempt.
  Revoking a device also leaves its client token-less, so it's swept the same way.
- **purges retired-adapter data** — if a connector is ever dropped, all its rows
  (accounts, tokens, clients, codes, usage) are deleted across the board. The
  retired set is an **explicit** list in the code, never inferred from
  configuration, so a missing env var can't accidentally wipe a live connector.

Both are logged only when they actually delete something — `cleanup-orphan-clients`
(`count`) and `cleanup-dead-adapter` (`adapter` + per-table counts) — so a quiet
log means there was nothing to clean. The one-hour threshold is a fixed constant,
not an env var, by design.

## How it works

1. Claude registers a client (DCR) and starts OAuth 2.1 (Authorization Code + PKCE).
2. On the authorize page the user signs in with Garmin (email + password, + MFA if
   prompted). The gateway logs in via `garminconnect`, stores **only the resulting
   tokens** (encrypted), and discards the password.
3. Claude exchanges the code for a Bearer token.
4. On each `/garmin/mcp` call the gateway ensures the user's `garmin_mcp` worker is running
   (its own tokens, bound to `127.0.0.1`) and reverse-proxies to it.

Adapters using the remote strategy replace steps 2 and 4 with a probe-verify
against the upstream MCP and a direct header-injected forward — no worker.

## Security

- Garmin passwords are never persisted.
- Tokens encrypted at rest (AES-256-GCM); the DB is useless without `GATEWAY_SECRET`.
- Bearer tokens stored only as SHA-256 hashes.
- OAuth 2.1 PKCE (S256), one-time 10-min codes, CSRF on forms, per-IP/-token rate limits.
- Workers bind `127.0.0.1` only; `garmin_mcp` is pinned to a reviewed commit.

> Deploy only on infrastructure you control and trust. Back up `DATA_DIR`; keep
> `GATEWAY_SECRET` separately.

## Before you deploy

- **Set a real random `GATEWAY_SECRET`** (`openssl rand -base64 48`) — the app
  refuses to start with the placeholder from `.env.example`.
- **Pin `GARMIN_MCP_REF` to a reviewed commit SHA** — `main` is a floating ref that
  can change without notice (supply-chain).
- **Revoking access** — access tokens expire after `ACCESS_TOKEN_TTL_DAYS` (default
  90; the user just re-authenticates in Claude). To revoke sooner — a leaked token
  or a removed user — run `python scripts/revoke.py --account [<adapter>:]<email>` (kill-switch
  for all of that account's tokens). A single device can be revoked with `--device <hash-prefix>` (prefixes are shown by `status.py`).
- **Run a manual end-to-end smoke test** with a real Garmin account (including the
  MFA path) before connecting real users — the upstream is mocked in the
  automated tests.

## Support

If this gateway is useful to you, you can [buy me a beer 🍺](https://buymeacoffee.com/venik).

## License

[MIT](LICENSE) © 2026 Vaclav Slajs

## Acknowledgements

Wraps the excellent [`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) by Taxuspt,
unmodified. Garmin and Garmin Connect are trademarks of Garmin Ltd.; this project is
not affiliated with or endorsed by Garmin.
