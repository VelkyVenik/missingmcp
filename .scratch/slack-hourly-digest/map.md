# Wayfinder map: Hourly production log digest → Slack

Label: wayfinder:map

## Destination

Two Slack reports live in production:

1. **Hourly health digest** — a GitHub Actions cron reads the gateway's **Railway
   logs via the Railway API**, runs an in-repo summarizer. Silent when healthy;
   expanded + `@here` on anomaly / liveness-probe failure.
2. **Daily user-stats report (~08:00)** — yesterday's **new / active / total users**
   (per-connector), **DB-derived** (`accounts.created_at`, `tool_usage.last_used`,
   `stats_counts` totals). Added 2026-07-18.

Done = both are **live and posting** (execution is carried into this map, not just
designed).

## Notes

- **Execution map** (Notes override of wayfinder's plan-only default): tickets build
  and ship the thing, not just decide it.
- **Locked decisions** (from the charting grill, 2026-07-18):
  1. Architecture: external scheduler reads Railway logs via API + posts to Slack
     (NOT an in-app lifespan digest — the app can't read its own stdout logs, where
     request statuses & re-auth events live).
  2. Host: GitHub Actions cron (`schedule`), free for the public repo, secrets in
     repo settings. Timing is best-effort (may drift minutes) — acceptable.
  3. Cadence: hybrid — always post, compact when healthy, escalate (`@here`) on
     anomaly.
- **Seeds to reuse:** the CLI monitor's summarizer already exists at
  `$CLAUDE_JOB_DIR/tmp/logsum.py` (levels / events / mcp-response status
  distribution / re-auth signals / errors / active accounts) — the digest logic is
  a direct adaptation. Repo convention: ops scripts live in `scripts/` (see README →
  Monitoring). Optional periodic-task pattern to mirror: `backup.py`
  (`enabled`/`due`/`run`, env-gated) — though here the scheduler is external.
- **Every code change goes via feature branch + PR** (CodeRabbit review); ask before
  creating the branch/PR. See [[branch-pr-workflow]]. Outward text (PR body) shown
  first, see [[approve-public-posts]].
- **Stateless windows:** each hourly run queries the last ~60 min of logs — no
  cross-run state file (unlike the CLI monitor's `since` boundary), if the Railway
  API supports a time window (ticket 01 confirms).

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [Railway logs API](issues/01-railway-logs-api.md) — `deploymentLogs(deploymentId,
  startDate, endDate, limit)` at backboard.railway.com/graphql/v2 with a Bearer
  `RAILWAY_API_TOKEN`; a TRUE 60-min time window exists; resolve the live deployment
  id via `serviceInstance{latestDeployment}`. Caveat: keep a JSON-parse-`message`
  fallback (our logs may not populate `attributes[]`). Asset:
  [assets/railway-logs-api.md](assets/railway-logs-api.md).
- [Anomaly thresholds](issues/03-anomaly-thresholds.md) — `@here` on ≥3 5xx/error
  or any critical (single blips noted, no ping); a **liveness HTTP probe** is the
  real down-signal (fail → `@here` anytime), zero-traffic alone never alerts;
  **silent when healthy** except one daily heartbeat. Cron still runs hourly.
  (Heartbeat hour moved 09:00 → **08:00** per ticket 07, to land beside the daily
  user-stats post.)
- [Daily user-stats report](issues/07-daily-user-stats-report.md) — **in-app**
  `DailyReport` (backup.py pattern, `due()` 08:00 Europe/Prague), per-connector
  new/active/total + weekly trend, **DB-derived** (not the log path). One shared
  Slack channel; webhook stored as a Railway env var (in-app) AND a GH secret
  (Action). Graduated → ticket 08 (build).

- [Build the in-app DailyReport](issues/08-build-in-app-daily-report.md) — SHIPPED
  (PR #2, deploy `5ed9609c`): `report.py` `DailyReport` + `scripts/daily_report.py`,
  posts daily 08:00 Europe/Prague, `SLACK_WEBHOOK_URL`-gated. Daily report done;
  hourly health digest (02/04/05/06) still open.

- [Summarizer + workflow + go-live](issues/06-go-live-verify.md) — SHIPPED
  (PR #3 + fix PR #4): `scripts/hourly_digest.py` + `.github/workflows/hourly-digest.yml`
  live on main, workflow **enabled**. Live dry-run against prod verified with the
  real project token (0 auth errors, correct silent verdict). Fix PR #4 resolved two
  bugs the dry-run caught: project-token header (`Project-Access-Token`, not Bearer)
  and JSON-encoded log-attribute values. **MAP COMPLETE** — both Slack reports live:
  daily user-stats 08:00 (in-app) + hourly health digest (GH Actions).

## Not yet specified

- Digest formatting polish (plain text vs Slack Block Kit) — decide during the
  summarizer build once the payload content is settled.

## Out of scope

- In-app lifespan digest (rejected in charting: can't read own stdout logs).
- Real-time / per-event paging or alerting faster than hourly.
- A metrics dashboard or long-term retention of the digests.
