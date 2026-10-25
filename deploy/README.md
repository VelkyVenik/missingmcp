# Production on hydra (Hetzner)

missingmcp.com moves from Railway to **hydra.venik.org** — a Hetzner CAX21
(4 vCPU Ampere arm64, 8 GB, Ubuntu 26.04), ~€16/mo instead of ~$55–60 on
Railway for the same load (avg 3.6 GB RAM, 0.3 vCPU, ~275 GB egress/mo).

```
Claude ─TLS─▶ Cloudflare (proxied) ─TLS, Origin CA cert─▶ Caddy :443 on hydra
           ─▶ 127.0.0.1:8080 ─▶ gateway container (docker compose) ─▶ workers 127.0.0.1:9000-9099
```

| File | On hydra |
|---|---|
| `docker-compose.yml` | `/opt/missingmcp/docker-compose.yml` |
| `deploy.sh` | `/opt/missingmcp/deploy.sh` (forced command of the CI deploy key) |
| `Caddyfile` | `/etc/caddy/Caddyfile` |
| — | `/opt/missingmcp/gateway.env` — app config + secrets, 0600, never in git |
| — | `/opt/missingmcp/data/` — the `/data` volume (`gateway.db`) |

## Deploys

`.github/workflows/deploy.yml` builds `ghcr.io/velkyvenik/missingmcp:<sha>`
(arm64) on every push to `main`. The deploy job SSHes to hydra with a key that
can only run `deploy.sh <sha>`: pull, recreate, wait for `/healthz`, roll back
to the previous tag if it doesn't come up. It runs on manual dispatch, and on
every push once the repo variable `HYDRA_AUTODEPLOY=true` — flipped at the
cutover, so Railway and hydra never both deploy production.

Manual deploy / rollback on hydra: `/opt/missingmcp/deploy.sh <sha>`.
Like on Railway, a deploy restarts the single node: a few seconds of downtime,
workers respawn lazily.

## Operating

```bash
ssh venik@hydra.venik.org
cd /opt/missingmcp
docker compose ps
docker compose logs -f --since 1h gateway                     # structured JSON
docker compose logs --since 24h gateway | grep '"event": "worker-login-rejected"'
docker compose exec gateway python3 -c "..."                  # replaces `railway ssh`
sqlite3 'file:data/gateway.db?mode=ro'                        # read-only prod DB
```

Logs rotate at 50 MB × 5 per container (`/etc/docker/daemon.json`). OS security
updates install automatically (`unattended-upgrades`); kernel updates still
need a manual `sudo reboot` (`/var/run/reboot-required`).

## Server setup (done 2026-10-25)

- User `venik` (sudo NOPASSWD, docker group); root and password SSH login off
  (`/etc/ssh/sshd_config.d/00-hardening.conf`). Hetzner Cloud Firewall: 22, 80, 443.
- Docker CE + compose plugin and Caddy from their upstream apt repos (Ubuntu's
  Caddy is 2.6), `sqlite3`, docker log rotation, Hetzner Backups on.

Still to do before the cutover:

1. **Origin certificate.** Cloudflare → SSL/TLS → Origin Server → Create
   certificate for `missingmcp.com` → save as
   `/etc/caddy/certs/missingmcp.com.{pem,key}` (dir 0750 root:caddy, key 0640).
   Check SSL/TLS mode is **Full (strict)**.
2. **Caddy:** copy `Caddyfile` to `/etc/caddy/Caddyfile`,
   `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
3. **App:** `sudo install -d -o venik -g venik /opt/missingmcp`, copy
   `docker-compose.yml` + `deploy.sh` (chmod 755), create `data/`, and
   `gateway.env` (0600) from `railway variables --kv` — drop `PORT`,
   `DATA_DIR` and the `RAILWAY_*` ones. **`GATEWAY_SECRET` must be identical**
   (it decrypts every stored token); `PUBLIC_URL` stays `https://missingmcp.com`.
4. **GHCR:** after the first build, make the `missingmcp` package public
   (GitHub → Packages → settings) — the repo is public and the image holds no
   secrets; otherwise `docker login ghcr.io` on hydra with a read-only PAT.
5. **Deploy key:** `ssh-keygen -t ed25519 -N '' -C github-deploy -f deploy_key`;
   add to `~venik/.ssh/authorized_keys` as
   `command="/opt/missingmcp/deploy.sh",restrict ssh-ed25519 AAAA… github-deploy`;
   store the private key as repo secret `HYDRA_DEPLOY_KEY`; delete the local copy.
6. **Dry run** (DNS still on Railway). `gateway.env` is production config, so
   first switch off everything with an outside effect — an empty-DB backup
   would overwrite the day's prod snapshot in the bucket:
   `printf 'services:\n  gateway:\n    environment:\n      BACKUP_S3_BUCKET: ""\n      SLACK_WEBHOOK_URL: ""\n      MAIL_API_TOKEN: ""\n      POSTHOG_API_KEY: ""\n' > docker-compose.override.yml`
   (delete it before the cutover). Run the workflow manually, then point a
   real client at hydra with `curl --resolve missingmcp.com:443:178.105.209.44 -k`
   (`/healthz`, `/.well-known/oauth-authorization-server/garmin`, `/`). Check
   in `docker compose logs` that the client IP is the real one, not 172.30.0.1
   or a Cloudflare IP. Before the cutover, `docker compose down` and empty `data/`
   so the real DB lands on a clean volume.

## Cutover (~10–15 min downtime)

Only one gateway may ever own the DB: both WHOOP and Garmin rotate refresh
tokens on use, so a token rotated by one copy is dead for the other. Every
hand-over is therefore *stop with persist → consistent snapshot → start the
other side* — never two live gateways, never an old snapshot.

1. **Park Railway.** Railway → gateway → Settings → **Custom Start Command** =
   `python3 scripts/railway_park.py`. The redeploy SIGTERMs the gateway (it
   persists rotated tokens — `worker-tokens-persisted trigger=shutdown` in the
   logs) and replaces it with a stub on the same volume that answers
   railway.json's `/healthz` check and 503s everything else. (`sleep infinity`
   would fail that healthcheck.) Wait until `railway deployment list` shows the
   new deployment `SUCCESS` and the previous one gone.
2. From the repo root: `sh deploy/pull_railway_db.sh` — refuses unless the
   container runs `railway_park.py` and no gateway process is left; consistent
   snapshot, checksum + `integrity_check` verified, lands in `./gateway.db`.
3. `scp gateway.db venik@hydra.venik.org:/opt/missingmcp/data/gateway.db`
   (hydra's `data/` empty, its gateway stopped), then on hydra
   `./deploy.sh <sha of main>`. Delete the local `gateway.db`.
4. Smoke test via `--resolve` (as in the dry run).
5. Cloudflare DNS: change the `missingmcp.com` record from the Railway CNAME to
   proxied **A 178.105.209.44** + **AAAA 2a01:4f8:c014:ef0c::1**. Proxied, so it
   switches at Cloudflare's edge within seconds — no TTL wait.
6. Verify in Claude: a Garmin and a WHOOP call on your own connectors (no
   reconnect needed — tokens, clients and Bearer hashes all came with the DB).
7. Set repo variable `HYDRA_AUTODEPLOY=true`; disconnect the Railway service
   from GitHub so pushes stop deploying there. Leave Railway parked.

## Rollback to Railway

The same hand-over in reverse — the Railway volume still holds the
pre-cutover DB, whose rotated-away tokens are already dead:

1. Repo variable `HYDRA_AUTODEPLOY=false` (no deploy may restart hydra midway).
2. On hydra: `cd /opt/missingmcp && docker compose stop gateway` (graceful —
   persists rotated tokens), then
   `sqlite3 data/gateway.db ".backup /tmp/gateway.db"` and copy it to the repo
   root: `scp venik@hydra.venik.org:/tmp/gateway.db .`
3. Railway must still be parked: `sh deploy/push_db_to_railway.sh` — refuses
   unless parked, uploads, verifies the checksum, swaps the file in atomically
   and drops the stale WAL.
4. Railway → clear the Custom Start Command (the gateway starts on the pushed
   DB); wait for the deployment `SUCCESS`.
5. Cloudflare DNS back to the Railway CNAME; reconnect the service to GitHub.
   Keep hydra's gateway stopped.

## After a few quiet days

- Delete the Railway service + volume.
- Backups: `BACKUP_S3_*` point at the Railway bucket — move them to Cloudflare
  R2 or Hetzner Object Storage before deleting it.
- Re-point operator tooling off Railway: `~/.config/missingmcp/prod-read/*`
  (→ `ssh venik@hydra.venik.org docker compose exec …`), the Grok morning
  status, `scripts/hourly_digest.py` / `daily_triage.py` (read Railway logs).
- Update `CLAUDE.md`, `README.md` → Deploy, remove `railway.json`.
