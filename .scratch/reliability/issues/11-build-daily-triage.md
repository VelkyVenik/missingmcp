# 11 — Build the daily triage and retire the hourly pings

Type: task
Status: claimed
Blocked by: 08

Execution ticket — the design is locked in
[08](08-autonomous-triage-design.md)'s answer (operator-grilled 2026-08-19).

## Question

Build and ship, per the 08 design:

1. **`scripts/daily_triage.py`** (standalone, httpx + stdlib like
   `hourly_digest.py` — the app can't read its own logs): pull the last 24 h
   from PostHog (logs + `$mcp_*` events; `POSTHOG_*` secrets), aggregate per
   error class with the deterministic signature table, call the Claude API
   (`ANTHROPIC_API_KEY`, versioned prompt in the script) for the analysis +
   proposed actions on the non-routine remainder, post to
   `SLACK_WEBHOOK_URL`: a one-line "all quiet" on healthy days, otherwise
   what happened → what it means → proposed action per class. Aggregates and
   masked accounts only — never MCP bodies or credentials (telemetry egress
   rule).
2. **`.github/workflows/daily-triage.yml`** — daily cron (~07:45 Europe/
   Prague, before the 08:00 user-stats post), same secrets pattern as
   hourly-digest.
3. **Retune `scripts/hourly_digest.py` to hard signals only**: loud on
   liveness-probe failure, `worker-died` rc≠0 across ≥2 distinct accounts in
   the hour, or any 5xx; drop minor posts, error counting (`ANOMALY_MIN`) and
   the hourly heartbeat (the daily post is the heartbeat). Keep event names
   stable; update README → Monitoring and the in-repo docs.
4. **Retire the PostHog "Worker start failures ≥3/hour" alert threshold** if
   it duplicates the retuned hourly (check what it keys on first — it caught
   the 2026-07-31 outage and must not lose that power).

Tests like `test_hourly_digest.py` for the pure parts (aggregation,
signature classification, verdicts, prompt assembly — the Claude call
mocked); dry-run against production data before enabling the workflow.
Feature branch + PR; new secrets provisioned by the operator (checklist in
the PR body).

Design constraint added 2026-08-19: keep ALL posting behind one small
`notify()` seam — the operator wants to move off Slack
([13](13-operator-channel.md)); the channel must be a config swap, not a
rewrite.

## Comments

- 2026-08-19: implemented on `feat/daily-triage`, **PR #19 open**
  (https://github.com/VelkyVenik/missingmcp/pull/19). Suite 384 passed.
  Resolves on merge + a successful dry-run of the workflow against
  production (needs new GH secrets POSTHOG_QUERY_KEY + ANTHROPIC_API_KEY —
  checklist in the PR body). PostHog "Worker start failures >=3/hour"
  alert kept as belt-and-braces.
