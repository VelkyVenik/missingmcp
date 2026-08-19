# 08 — Design the autonomous triage mechanism (ping = proposed action)

Type: grilling
Status: resolved
Blocked by: 01

## Question

Replace "there are N errors" pings with a mechanism that analyzes problems
and messages the operator **only when action is needed** — carrying the
diagnosis and a proposed next step, not a raw count. The operator's framing:
"an autonomous mechanism that analyzes the problems and proposes what to do
next; write to me only with actions, not all day that errors exist."

Decide, informed by [01](01-digest-error-breakdown.md)'s error breakdown:

- **Where it runs** — extend `scripts/hourly_digest.py` with an analysis
  step, a scheduled Claude agent, or something else entirely.
- **What it reads** — Railway logs, PostHog, the production DB.
- **When it may message** — known signature → mapped recommended action;
  unknown pattern → deeper analysis before any ping; below-threshold noise →
  silence or a daily/weekly summary. The daily heartbeat's fate.
- **Message format** — what happened → what it means → proposed action.
- **Autonomy boundary** — the mechanism proposes, it never auto-remediates
  (out of scope for this map); whether it may file `.scratch/` tickets is a
  separate follow-up question (see the map's Not yet specified).

The implementation becomes a follow-up ticket once this decides the shape.

## Answer (2026-08-19) — the design, decided with the operator

**Shape: a daily GitHub Actions triage + an hourly hard-signal pager.** All
grilled 2026-08-19; the operator's 2026-08-19 directive (retire hourly "N
errors" pings, replace with a daily analysis with proposals) is the frame.

1. **Runtime — GitHub Actions cron + Claude API + Slack webhook**
   (`scripts/daily_triage.py` + `.github/workflows/daily-triage.yml`,
   mirroring the proven hourly-digest pattern). The script collects and
   pre-aggregates the day's data, calls the Claude API for classification,
   impact assessment and PROPOSED ACTIONS, and posts the result via
   `SLACK_WEBHOOK_URL`. Versioned prompt in the repo; new secret
   `ANTHROPIC_API_KEY`. A cloud routine (CCR) was rejected: it failed to
   deliver ticket 06's run on 2026-08-07 and its prompt isn't versioned.
2. **Escalation — the hourly workflow survives, hard signals only.** It goes
   loud (`<!here>`) ONLY on: liveness probe failure (web down — the one
   signal no log pipeline can see), a `worker-died` rc≠0 burst across
   multiple distinct accounts (the validated 2026-07-31 image-fault
   signature), or any 5xx. No error counting, no minor posts, no hourly
   heartbeat — the daily post is the heartbeat/dead-man switch: a short
   "all quiet" line on healthy days, the full analysis otherwise.
3. **Data — PostHog only** (logs + `$mcp_*` events; the same surface both
   incidents were diagnosed on). The script hands Claude aggregates + a few
   sanitized samples, never raw dumps; known-signature classification happens
   deterministically in the script (credential-expiry, upstream-Garmin
   flakiness, auth-flow noise, `mcp-stream-interrupted` POST-side surge,
   gateway faults), Claude reasons about the remainder and drafts proposals.
4. **Autonomy — Slack-only proposals.** No ticket filing, no repo writes from
   CI; the operator (or a Claude session on request) turns proposals into
   tickets. Egress rule holds: aggregates + masked accounts only, never MCP
   bodies or credentials.
5. **Format** (locked by the directive): what happened → what it means →
   proposed action, per class, most actionable first.

Implementation graduated into
[11 — Build the daily triage](11-build-daily-triage.md); the operator's
directive amends the map's destination — the mechanism is to be BUILT, not
just spec'd.

## Comments

- 2026-08-19 (operator directive, narrows this design): **"Cancel the hourly
  Slack messages — useless when they page every hour that something's broken.
  Replace with something meaningful: a daily analysis with proposals, or
  similar."** Locked-in consequences for the design: (1) the hourly digest's
  `<!here>`/minor alerting role is to be RETIRED once the replacement exists
  (the daily heartbeat's fate is part of this ticket); (2) cadence of the
  triage output is daily (with an escalation path for true emergencies like
  the 2026-07-31 outage — decide its shape here); (3) format stays "what
  happened → what it means → proposed action".

- 2026-07-31: live validation of one signature family — the mcp 2.0.0 worker
  outage (every spawn `worker-died` rc=1 on `ModuleNotFoundError`; see the
  `fix(docker): pin mcp<2` commit). The PostHog "Worker start failures
  ≥3/hour" alert correctly paged (33/h), the operator pasted it in, and the
  diagnosis needed: error-class breakdown → one account's full worker-log
  timeline → the crash traceback → Dockerfile. Exactly the analyze-then-
  propose loop this ticket wants to automate. Signature material: a burst of
  `worker-died` rc≠0 across MANY distinct accounts right after a deploy =
  gateway/image fault, page with the traceback and the suspect deploy; the
  same event on ONE account = maybe that account, hold. Also note the
  cascade: users answer the re-auth 401 and re-sign-in in vain — a triage
  message should distinguish "credentials problem" from "our fault" fast.
