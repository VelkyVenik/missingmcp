# 01 — Clean up Slack

Type: task
Status: needs-triage
Blocked by: —

## Context

Operator monitoring moved off Slack (2026-10-28): the Grok bot sends the morning status, and
uptime + host alerts go to ntfy on hydra. The GHA pager and triage jobs are already deleted.
What still talks to Slack or mentions it (2026-10-28):

- `src/missingmcp/report.py` — the daily per-connector user-stats report, posted to
  `SLACK_WEBHOOK_URL` from the lifespan loop.
- `src/missingmcp/config.py` — `slack_webhook_url`, `daily_report_hour`, `daily_report_tz`.
- `scripts/daily_report.py`, `tests/test_report.py`.
- `README.md` (env-var reference, Monitoring) and `CLAUDE.md` (the `report.py` line).
- `src/missingmcp/pages.py` — only a comment (Slack among the og:image scrapers); keep it.
- On hydra: the `SLACK_WEBHOOK_URL` env var, if still set.

## Open question

Remove the daily stats report entirely, or move it to the new channel (Grok / ntfy)?
Does the Grok morning status already cover the per-connector numbers?

## Done when

- No code path posts to Slack; the env vars and their docs are gone (or replaced).
- `SLACK_WEBHOOK_URL` removed from hydra's env.
- Tests pass.
