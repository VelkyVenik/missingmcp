# 08 — Design the autonomous triage mechanism (ping = proposed action)

Type: grilling
Status: open
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
