# 07 — Daily user-stats morning report (~08:00)

Type: grilling
Status: resolved

## Question

A daily morning post (~08:00 Europe/Prague) with yesterday's user numbers. Two
things to decide:

### A. Content

Confirm the fields and grouping. Candidate set (all DB-derivable):
- **New users** yesterday — `COUNT(accounts WHERE created_at in yesterday)`, per
  adapter (garmin/whoop). This is the number the operator specifically asked for; it
  is **only reliable from the DB** — logs can't tell a new signup from a re-auth
  (a re-auth emits the same `authorize-finish`/`token-issued`, cf. the Pavla case).
- **Active users** yesterday — distinct accounts with `tool_usage.last_used` (or a
  request) in yesterday's window.
- **Total users** — `stats_counts` (`accounts`, `people_with_token`), per adapter.
- Optional: top tools yesterday, most-active account, new-vs-total trend.

### B. Architecture (the real fork)

Because this is **DB-derived**, it does NOT fit the hourly digest's log-reading GH
Action. Options:
- **In-app, lifespan loop (likely best):** a `DailyReport` class mirroring
  `backup.py` (`enabled`/`due`/`run`, gated on `SLACK_WEBHOOK_URL`), computing
  straight from the DB it already holds and POSTing to Slack at the 08:00 tick. No
  CI, no DB-from-outside problem. (The "in-app was rejected" note in the map applies
  only to the *log-derived* hourly digest — not this.)
- **GH Action hitting a new authenticated stats endpoint** on the gateway (app still
  computes from DB; CI just curls it). Keeps all Slack posting in one place (the GH
  Action) but adds an endpoint + auth.
- **GH Action via `railway ssh`** running `scripts/status.py`/`usage.py`-style DB
  queries. Reuses existing scripts but needs the railway CLI + SSH key in CI.

Also decide: should this 08:00 report **absorb the hourly digest's ~09:00 health
heartbeat** into one morning message ("system healthy + yesterday's users"), or stay
separate? And one shared Slack webhook/channel vs. a separate channel.

Resolution graduates the implementation ticket(s) for whichever architecture wins.

## Answer

Resolved 2026-07-18 (grilling with the operator).

- **Architecture: in-app**, from the lifespan loop. A `DailyReport` class mirroring
  `backup.py` (`enabled`/`due`/`run`, gated on `SLACK_WEBHOOK_URL`), `due()` at
  **08:00 Europe/Prague**, computes straight from the DB and POSTs to Slack. No CI,
  no external DB access. Timezone-aware "yesterday" window. (The map's "in-app
  rejected" note applies only to the log-derived hourly digest.)
- **Content: per-connector (garmin/whoop) — new yesterday / active yesterday /
  total**, plus a grand-total line, plus a **weekly trend** (new over the last 7
  days and/or vs. the prior day). Sources: `accounts.created_at` (new),
  `tool_usage.last_used` (active), `stats_counts`/account counts (total). NOT daily
  call volume — `tool_usage.calls` is cumulative, so per-day call counts live in the
  hourly log digest, not here.
- **Coordination: one Slack channel/webhook**, shared with the hourly digest. Both
  morning posts land ~08:00 — the app's user-stats **and** the health heartbeat,
  which **moves from ~09:00 to the 08:00 GH Action run**. Anomalies / probe failures
  post anytime. Each pipeline's morning post proves that pipeline is alive.
- **Webhook lives in two places (same URL):** a **Railway env var**
  `SLACK_WEBHOOK_URL` (for the in-app DailyReport) **and** a GH secret (for the
  hourly Action) — folded into ticket 02.

Graduates → ticket 08 (build the in-app DailyReport). Delivered via branch + PR.
