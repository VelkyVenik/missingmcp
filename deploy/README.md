# Production on hydra (Hetzner)

missingmcp.com runs on **hydra.venik.org** — a Hetzner CAX21 (4 vCPU Ampere
arm64, 8 GB, Ubuntu 26.04), ~€16/mo flat (avg 3.6 GB RAM, 0.3 vCPU,
~150–275 GB egress/mo). See [History](#history) for the move off Railway.

```
Claude ─TLS─▶ Cloudflare (proxied) ─TLS, Origin CA cert─▶ Caddy :443 on hydra
           ─▶ 127.0.0.1:8080 ─▶ gateway container (docker compose) ─▶ workers 127.0.0.1:9000-9099
```

| File | On hydra |
|---|---|
| `docker-compose.yml` | `/opt/missingmcp/docker-compose.yml` |
| `deploy.sh` | `/opt/missingmcp/deploy.sh` (forced command of the CI deploy key) |
| `Caddyfile` | `/etc/caddy/Caddyfile` (missingmcp.com answers Cloudflare IPs only — direct hits get 403) |
| — | `/opt/missingmcp/gateway.env` — app config + secrets, 0600, never in git |
| — | `/opt/missingmcp/data/` — the `/data` volume (`gateway.db`) |

## Deploys

`.github/workflows/deploy.yml` builds `ghcr.io/velkyvenik/missingmcp:<sha>`
(arm64) on every push to `main`. The deploy job SSHes to hydra with a key that
can only run `deploy.sh <sha>`: pull, recreate, wait for `/healthz`, roll back
to the previous tag if it doesn't come up. It runs on manual dispatch, and on
every push while the repo variable `HYDRA_AUTODEPLOY=true` (set it to `false`
to hold deploys during maintenance).

Manual deploy / rollback on hydra: `/opt/missingmcp/deploy.sh <sha>`.
A deploy restarts the single node: a few seconds of downtime,
workers respawn lazily.

## Operating

```bash
ssh venik@hydra.venik.org
cd /opt/missingmcp
docker compose ps
journalctl -t missingmcp-gateway -f -o cat                    # structured JSON, live
journalctl -t missingmcp-gateway --since -24h -o cat | grep '"event": "worker-login-rejected"'
docker compose exec gateway python3 /app/scripts/status.py   # ops scripts (README → Monitoring)
sqlite3 'file:data/gateway.db?mode=ro'                        # read-only prod DB
```

### Monitoring access (Grok bot)

User `grok` (key-only SSH, not in the docker group) may sudo exactly one
command, `/usr/local/bin/mmcp` (= `deploy/mmcp`, `/etc/sudoers.d/91-mmcp`):
`sudo mmcp logs [6h|1d] [--grep TEXT]`, `status`, `metrics [1d..28d]`, `egress`,
`workers`, `usage`, `report`, and `sql` (one statement on stdin, DB opened `?mode=ro` +
`query_only`). After editing `deploy/mmcp`:
`scp deploy/mmcp venik@hydra.venik.org:/tmp/ && ssh venik@hydra.venik.org sudo install -m 755 /tmp/mmcp /usr/local/bin/mmcp`.

Host metrics: sysstat samples CPU, RAM,
network and load every minute (`sysstat-collect.timer.d/every-minute.conf`)
and keeps 28 days (`HISTORY=28` in `/etc/sysstat/sysstat`); `mmcp metrics`
summarizes them per hour (1d) or per day.

Dashboard: **Beszel** at https://hydra.venik.org (`/opt/beszel`, not in this
repo — hub 0.21.0 on `127.0.0.1:8090` behind Caddy with a Let's Encrypt cert,
agent on a unix socket shared only with the hub, `docker.sock` read-only).
Host + per-container CPU/RAM/swap/network history and alerts. Login
`admin@venik.org`, password in `/opt/beszel/hub.env`.

### Alerts (ntfy push to the phone)

All alerts go to one ntfy.sh topic (app: ntfy). The topic name works like a
password — it is **not** in this public repo: see `TOPIC` in
`~/uptime/check.sh` on dragon (also the Beszel notification URL).

- **Uptime** — `~/uptime/check.sh` on **dragon** (cron, every minute; another
  host and location, so it still fires when hydra is down): `GET
  https://missingmcp.com/healthz`, DOWN push (urgent) after 2 consecutive
  failures, UP push on recovery; failed checks in `~/uptime/failures.log`.
- **Beszel** (Settings → Notifications, shoutrrr `ntfy://ntfy.sh/<topic>`):
  hydra down / RAM > 85 % 5 min / CPU > 80 % 10 min / disk > 80 %; dragon down /
  RAM > 90 % 10 min / disk > 85 %. Beszel has no swap alert — swap shows in
  `mmcp metrics`.
- Blind spot: each host watches the other, so a Hetzner-wide outage taking
  both down sends nothing.

The gateway logs to the host's **persistent journal** (compose `logging:
journald`, tag `missingmcp-gateway`), not to a per-container file: `docker
logs` history dies with the container on every deploy, the journal spans
them. Capped at 10 GB, compressed, 5 GB disk always kept free, rate limit
raised for bursts (`/etc/systemd/journald.conf.d/50-missingmcp.conf`) — weeks
to months of history. Other containers keep the daemon default of
50 MB × 5 (`/etc/docker/daemon.json`). OS security updates
install daily 06:00–07:00 UTC (`unattended-upgrades`); when one needs a reboot
(kernel, libc, systemd) the host reboots at the next **03:30 UTC**
(`/etc/apt/apt.conf.d/52-auto-reboot`) — ~1 min down, the gateway gets 30 s to
persist tokens, containers come back via `restart: unless-stopped`. Cancel a
pending one with `sudo shutdown -c`.

## Server setup (done 2026-10-25)

- User `venik` (sudo NOPASSWD, docker group); root and password SSH login off
  (`/etc/ssh/sshd_config.d/00-hardening.conf`). Hetzner Cloud Firewall: 22, 80, 443.
- Docker CE + compose plugin and Caddy from their upstream apt repos (Ubuntu's
  Caddy is 2.6), `sqlite3`, docker log rotation, Hetzner Backups on.
- 4 GB `/swapfile` (fstab), `vm.swappiness=10` (`/etc/sysctl.d/90-swappiness.conf`):
  a cushion, not capacity — the container may swap up to its 6 GB limit
  (`memory.swap.max`), so sustained swap use in `mmcp metrics` means it's time
  to lower `MAX_WORKERS` or move to a CAX31.

## Backups

**Hetzner Backups** (server setting): a daily snapshot of the whole VM, the
last 7 kept — `data/gateway.db` and `gateway.env` included. The gateway's own
off-box S3 backup (`BACKUP_S3_*`) is **off** here: it wrote to the Railway
bucket and was dropped with Railway (2026-10-28). Restore = Hetzner console →
hydra → Backups → restore (whole VM), or rebuild a fresh server from a
snapshot and copy `data/gateway.db` + `gateway.env` out of it. Keep a copy of
`GATEWAY_SECRET` in the password manager too: without it the DB's account
blobs can't be decrypted.

## Rebuilding the server

On a fresh Ubuntu box after the setup above:

1. **Origin certificate.** Cloudflare → SSL/TLS → Origin Server → Create
   certificate for `missingmcp.com` → save as
   `/etc/caddy/certs/missingmcp.com.{pem,key}` (dir 0750 root:caddy, key 0640).
   Check SSL/TLS mode is **Full (strict)**.
2. **Caddy:** copy `Caddyfile` to `/etc/caddy/Caddyfile`,
   `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
3. **App:** `sudo install -d -o venik -g venik /opt/missingmcp`, copy
   `docker-compose.yml` + `deploy.sh` (chmod 755), create `data/` (restore
   `gateway.db` into it from a backup) and `gateway.env` (0600).
   **`GATEWAY_SECRET` must be identical** (it decrypts every stored token);
   `PUBLIC_URL` stays `https://missingmcp.com`.
4. **GHCR:** the `missingmcp` package is public (the repo is public and the
   image holds no secrets), so hydra pulls without a login.
5. **Deploy key:** `ssh-keygen -t ed25519 -N '' -C github-deploy -f deploy_key`;
   add to `~venik/.ssh/authorized_keys` as
   `command="/opt/missingmcp/deploy.sh",restrict ssh-ed25519 AAAA… github-deploy`;
   store the private key as repo secret `HYDRA_DEPLOY_KEY`; delete the local copy.
6. Only one gateway may ever own the DB: both WHOOP and Garmin rotate refresh
   tokens on use, so a token rotated by one copy is dead for the other. Stop
   the old gateway (`docker compose stop gateway` persists rotated tokens)
   before taking the DB it hands over.
7. Check the backend over SSH — the origin answers only Cloudflare, so a
   `curl --resolve …` from outside gets **403 by design**:
   `ssh venik@hydra.venik.org 'for p in /healthz /.well-known/oauth-authorization-server/garmin /; do curl -s -o /dev/null -w "%{http_code} $p\n" http://127.0.0.1:8080$p; done'`.
   Then point Cloudflare DNS (proxied A/AAAA) at the new server and check
   `/healthz` plus the per-IP rate limit through Cloudflare: 30
   unauthenticated `POST /garmin/mcp` from one IP → 429, a second IP still
   401; a spoofed `X-Forwarded-For` must not help.

## History

Until 2026-10-25 missingmcp.com ran on Railway (~$42–45/mo and rising with
usage). The cutover parked the Railway service, moved a consistent DB snapshot
here and switched Cloudflare DNS; Railway stayed parked as the rollback until
it was deleted on 2026-10-28. The cutover/rollback tooling
(`scripts/railway_park.py`, `deploy/pull_railway_db.sh`,
`deploy/push_db_to_railway.sh`) lives in git history.
