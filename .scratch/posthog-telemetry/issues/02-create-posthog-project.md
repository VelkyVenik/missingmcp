# 02 — Create the EU PostHog project + keys

Type: task
Status: resolved

## Question

HITL: create the `missingmcp` project on eu.posthog.com (same org as WakePins if that org
is EU; otherwise a new org — operator's call at creation time), and capture:

- the Project API key (public, for ingestion + posthog-js),
- a Personal API key scoped for the MCP server (ticket 06),
- where each secret lives (Railway env vars for the gateway; operator's password manager
  for the personal key).

Resolve with what was created and where the keys live (never the keys themselves).

## Answer

Resolved by the operator, 2026-07-19:

- The PostHog project for missingmcp exists (created by the operator; EU cloud per the
  charting decision — verify the region reads back correctly once the MCP connection
  works, ticket 06).
- The **Project API key** (public `phc_…` ingestion token) was handed over in-session.
  It is NOT recorded here by design; it lives in the PostHog project settings
  (retrievable anytime) and will be set as a Railway env var when the implementation
  that reads it lands (env var name is the spec's call, ticket 07).
- The **Personal API key** item is deferred to ticket 06: the claude.ai PostHog
  connector now authenticates via OAuth (mcp.posthog.com), so a personal key may be
  unnecessary — create one only if the OAuth connector turns out insufficient.
