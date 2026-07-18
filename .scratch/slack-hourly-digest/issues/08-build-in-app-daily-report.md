# 08 — Build the in-app DailyReport

Type: task
Status: resolved
Blocked by: 02

## Answer

Shipped 2026-07-18 via PR #2 (squash `59a34511`), deploy `5ed9609c` SUCCESS,
healthy boot (gateway-started, 0 errors). `src/missingmcp/report.py`
(`DailyReport` + `build_report`/`render_slack`/`post_slack`/`open_ro`),
`scripts/daily_report.py` (print / `--post`), store helpers, config
(`slack_webhook_url`/`daily_report_hour`/`daily_report_tz`), lifespan wiring.
Tests: `tests/test_report.py` (9). CodeRabbit: one nitpick (SQLite URI via
`Path.as_uri()`) fixed in `6f5bba0`. First auto-post tomorrow 08:00 Europe/Prague
(today skipped by the redeploy-guard, as designed).

## Question

Implement the daily user-stats report as an in-app feature (decisions in ticket 07),
delivered via feature branch + PR (CodeRabbit).

- New module `src/missingmcp/report.py` (or similar): a `DailyReport` class mirroring
  `backup.py` — `enabled` (gated on `SLACK_WEBHOOK_URL` env), `due(now)` (fires once
  at **08:00 Europe/Prague**), `run()` (blocking → call via `asyncio.to_thread`;
  never raises, mirrors `Backup.run`).
- Config: add `slack_webhook_url` + any tunables (report hour, tz) to `config.py`,
  read from env; disabled when the webhook is unset.
- Metrics from the DB (add `store` helpers, tested): per adapter (garmin/whoop) —
  **new** (`accounts.created_at` within yesterday, Europe/Prague day bounds),
  **active** (distinct accounts with `tool_usage.last_used` within yesterday),
  **total** (account / people-with-token counts); plus a **weekly trend** (new over
  last 7 days and/or vs. prior day). Grand-total line.
- Compose a Slack message (formatting — plain vs Block Kit — map fog) and POST to the
  webhook via httpx (reuse the backup.py dependency-free style).
- Wire into the lifespan loop next to backup/cleanup (`due()`-gated,
  `to_thread`-run). Log an event on post (stable schema).
- Tests: `store` metric helpers against a seeded DB (day-boundary/timezone edges),
  `DailyReport.due()` timing, and the message body for a sample dataset.

- **Also runnable as a standalone script** (operator request 2026-07-18): a
  `scripts/daily_report.py` (matching the `scripts/usage.py` convention — read-only
  DB, `resolve_db()`, argparse) that computes and **prints** the report by default
  (`--post` to actually send to the webhook, `--now`/`--date` to test a specific
  day). Both the in-app `DailyReport` and the script call the **same**
  `build_report` / `render_slack` functions, so testing the script tests the real
  output. Runnable locally or via `railway ssh`.

Note: the health heartbeat's move to 08:00 lives in the hourly-digest side (tickets
03/04), not here. Secret status: `SLACK_WEBHOOK_URL` is set in Railway (in-app) and
GitHub (Action) as of 2026-07-18 — this ticket is unblocked for a live run.
