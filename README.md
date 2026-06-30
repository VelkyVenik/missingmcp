# Garmin MCP Gateway

A multi-user, OAuth 2.1–protected gateway that lets a small trusted circle each
connect their own [Garmin Connect](https://connect.garmin.com) account to Claude
(iOS, Android, Web, Desktop). It wraps the **unmodified**
[`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) worker and adds OAuth,
per-user token isolation, and a reverse proxy.

```
Claude → POST /mcp (Bearer) → Gateway → 127.0.0.1:<port>/mcp (per-user garmin_mcp) → connect.garmin.com
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
- **Password is never stored** — used once to sign in with Garmin (MFA supported);
  only the resulting session tokens are persisted.
- **Encrypted at rest** — tokens sealed with AES-256-GCM; the DB is useless without
  `GATEWAY_SECRET`. Bearer tokens are stored only as SHA-256 hashes.
- **Per-user isolation** — each account gets its own `garmin_mcp` worker bound to
  `127.0.0.1`, started on demand and reaped when idle.
- **Hardened** — one-time 10-min auth codes, CSRF on forms, per-IP/-token rate
  limits, the `garmin_mcp` worker pinned to a reviewed commit.
- **Instructional landing page** served on `/` and as a friendly fallback for
  unknown paths.

## Quick start (Docker)

```bash
cp .env.example .env          # set GATEWAY_SECRET, PUBLIC_URL, pin GARMIN_MCP_REF
docker compose up -d --build
```

Put nginx in front for TLS + your domain (see [`nginx.conf.example`](nginx.conf.example)),
then add `https://<your-domain>/mcp` as a remote MCP server in Claude.

## Local development

```bash
uv pip install -e ".[dev]"
uv run --extra dev pytest -q                 # run the test suite

# Run the gateway locally (no Garmin account needed to exercise the OAuth surface).
# garmin-mcp isn't on PATH locally, so point GARMIN_MCP_CMD at uvx.
GATEWAY_SECRET="$(openssl rand -base64 48)" \
PUBLIC_URL=http://localhost:8088 PORT=8088 DATA_DIR=./.localdata \
GARMIN_MCP_CMD="uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp" \
  uv run garmin-gateway
```

A `.env` file in the working directory is loaded automatically (real environment
variables take precedence), so you can drop the same values there instead.

## Connecting from Claude

1. In any Claude client: **Settings → Connectors → Add custom connector**, or in
   the CLI: `claude mcp add --transport http garmin https://<your-domain>/mcp`.
2. Claude opens the gateway's sign-in page — enter your Garmin Connect email +
   password (and an MFA code if prompted).
3. Done — your Garmin tools are now available in Claude.

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
| `GARMIN_MCP_REF` | no | `main` | Docker build arg: commit/ref of `garmin_mcp` to install. Pin to a SHA. |
| `WORKER_PORT_START` / `WORKER_PORT_END` | no | `9000` / `9099` | Port range for per-user workers. |
| `WORKER_IDLE_TTL` | no | `900` | Seconds before an idle worker is reaped. |
| `WORKER_STARTUP_TIMEOUT` | no | `20` | Seconds to wait for a worker to become healthy. |
| `MAX_WORKERS` | no | `10` | Max concurrent per-user workers. |
| `OPERATOR_NAME` / `OPERATOR_EMAIL` | no | — | Shown on the landing page. |
| `GATEWAY_LOG_FILE` | no | — | If set, tees structured + stdlib logs to this file. |
| `GATEWAY_LOG_LEVEL` | no | `info` | `debug`\|`info`\|`warning`\|`error`\|`critical`. `debug` is verbose (logs garminconnect/urllib3 internals) — avoid in production. |

## Monitoring

Two helper scripts read the gateway's state:

```bash
python scripts/status.py          # snapshot: people with a token, devices/clients
                                  #   connected, registered clients, running workers
python scripts/monitor.py         # live tail of structured events
python scripts/monitor.py --all   # include garminconnect/urllib3 debug noise
```

**With Docker** the scripts are baked into the image at `/app/scripts`; run them
inside the container. `status.py` finds the DB under `/data` automatically:

```bash
docker compose exec gateway python /app/scripts/status.py
docker compose exec gateway python /app/scripts/monitor.py --file /data/gateway.log
docker compose logs -f gateway                       # live events (simplest)
docker compose logs -f gateway | grep '"event": "stats"'
```

> `monitor.py` reads `GATEWAY_LOG_FILE` (pass `--file` if it isn't set). Inside a
> container the logs also go to stdout, so `docker compose logs -f` is usually the
> easiest live view.

The gateway also logs a `stats` event (accounts / tokens / people-with-token /
clients / active-workers) on startup and whenever those counts change, and
`status.py` lists the running per-user `garmin_mcp` workers.

## How it works

1. Claude registers a client (DCR) and starts OAuth 2.1 (Authorization Code + PKCE).
2. On the authorize page the user signs in with Garmin (email + password, + MFA if
   prompted). The gateway logs in via `garminconnect`, stores **only the resulting
   tokens** (encrypted), and discards the password.
3. Claude exchanges the code for a Bearer token.
4. On each `/mcp` call the gateway ensures the user's `garmin_mcp` worker is running
   (its own tokens, bound to `127.0.0.1`) and reverse-proxies to it.

## Security

- Garmin password is never persisted.
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
- **Access tokens have no expiry or auto-revocation** — to revoke a device, delete
  its row from the `access_tokens` table.
- **Run a manual end-to-end smoke test** with a real Garmin account (including the
  MFA path) before connecting real users — the `garminconnect` login/token path is
  mocked in the automated tests.

## License

[MIT](LICENSE) © 2026 Vaclav Slajs

## Acknowledgements

Wraps the excellent [`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) by Taxuspt,
unmodified. Garmin and Garmin Connect are trademarks of Garmin Ltd.; this project is
not affiliated with or endorsed by Garmin.
