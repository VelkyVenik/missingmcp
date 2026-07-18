# 05 — GitHub Actions hourly cron workflow

Type: task
Status: resolved
Blocked by: 02, 04

## Question

Add the workflow (`.github/workflows/hourly-digest.yml`) that runs the summarizer
(ticket 04) every hour:

- `on: schedule: cron: '0 * * * *'` + `workflow_dispatch` (manual trigger for
  testing).
- Inject `RAILWAY_API_TOKEN` + `SLACK_WEBHOOK_URL` from repo secrets (ticket 02).
- Set up Python, run the script (no `--dry-run` in the scheduled path;
  `workflow_dispatch` can pass an input to dry-run).
- Concurrency guard so overlapping runs don't double-post; sensible timeout.

Delivered via feature branch + PR (CodeRabbit). Note GH cron is best-effort timing.
