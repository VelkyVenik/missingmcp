# 02 — Provision the CI secrets (Railway token + Slack webhook)

Type: task
Status: resolved
Blocked by: 01

## Answer

Done 2026-07-18. `SLACK_WEBHOOK_URL` set as a **Railway env var** (for the in-app
daily report, shipped) AND a **GitHub repo secret** (for the hourly digest Action).
`RAILWAY_API_TOKEN` set as a **GitHub repo secret** (workspace/account token, Bearer,
reads deployment logs). One shared Slack channel.

## Question

Get the two secrets the GitHub Actions job needs, stored as **repo secrets** on
`VelkyVenik/missingmcp` (HITL where the portals need the human):

1. **Railway API token** — per ticket 01, an **account or workspace token** (NOT a
   project token — unconfirmed for logs), used as `Authorization: Bearer`. Prefer a
   **workspace token** for the narrowest scope that can still read the gateway's
   deployment logs. Store as GH secret `RAILWAY_API_TOKEN`. Record type/scope.
2. **Slack incoming webhook** for the target channel (one shared channel for both
   reports, per ticket 07). Create it in Slack (Incoming Webhooks app), then store
   the SAME URL in **two places**:
   - a **GH repo secret** `SLACK_WEBHOOK_URL` (for the hourly digest Action), and
   - a **Railway env var** `SLACK_WEBHOOK_URL` on the gateway service (for the in-app
     daily user-stats report, ticket 08 — it reads it from the app environment).
   Record the channel name.

Answer records: GH secret names, Railway token type/scope, Slack channel, and any
surprises. Both also mirrored into the local `.env` skeleton for reference (never
committed). Note: don't paste the secret values into the ticket.
