# 04 — In-repo summarizer + Slack poster

Type: task
Status: resolved
Blocked by: 02

Note: tickets 01 and 03 are resolved — their decisions are baked into the steps
below. This ticket is now blocked only by **02** (needs a real `RAILWAY_API_TOKEN`
to fetch logs and to run the one-time `message`/`attributes[]` validation). It can
be *built* against sample fixtures with `--dry-run` before 02 lands; wire the live
token last.

## Question

Build the script the cron runs (in `scripts/`, per repo convention). It should:

1. Resolve the live deployment id (`serviceInstance{latestDeployment}`) and fetch
   the last 60 min via `deploymentLogs(startDate=now−60m, endDate=now, limit=5000)`
   at `backboard.railway.com/graphql/v2` with `Authorization: Bearer $RAILWAY_API_TOKEN`
   (ticket 01). **Keep a JSON-parse-`message` fallback** if structured fields don't
   land in `attributes[]` — validate once against prod (ticket 01 watch-out).
2. Compute the digest from the structured rows — adapt the seed
   `$CLAUDE_JOB_DIR/tmp/logsum.py` (levels, event counts, `mcp-response` status
   distribution, worker churn, re-auth signals, error rows, distinct active accounts).
3. **Liveness probe:** one HTTP GET to the gateway public URL (landing / a well-known
   endpoint); a failed probe is the down-signal.
4. **Verdict (ticket 03):** `@here` + expanded when ≥3 5xx/error OR any critical OR
   probe failed; single blips noted without `@here`; re-auth signals are NOT
   anomalies. **Post-gate:** POST only on anomaly, probe failure, or the daily
   **08:00 Europe/Prague** heartbeat run — otherwise stay **silent** (healthy hours
   don't post). The script decides "is this the heartbeat hour" from the current
   time. (08:00 per ticket 07, so the health heartbeat lands beside the in-app daily
   user-stats post in the same channel.)
5. POST to the Slack incoming webhook (plain text vs Block Kit decided here — map fog).
6. Support `--dry-run` (print, don't post) + env-based secrets, so it's buildable and
   locally runnable before CI secrets exist. Tests: verdict logic against sample log
   fixtures.

Delivered via feature branch + PR (CodeRabbit). Tests where practical (verdict
logic against sample log fixtures). Records: script path, its env-var contract.
