# 02 — Create the EU PostHog project + keys

Type: task
Status: open

## Question

HITL: create the `missingmcp` project on eu.posthog.com (same org as WakePins if that org
is EU; otherwise a new org — operator's call at creation time), and capture:

- the Project API key (public, for ingestion + posthog-js),
- a Personal API key scoped for the MCP server (ticket 06),
- where each secret lives (Railway env vars for the gateway; operator's password manager
  for the personal key).

Resolve with what was created and where the keys live (never the keys themselves).
