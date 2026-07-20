# Wayfinder map: PostHog telemetry

Label: wayfinder:map

## Destination

An implementation-ready design spec at `docs/superpowers/specs/<date>-posthog-telemetry-design.md`:
missingmcp.com usage statistics and logs flow to PostHog (server-side events + web analytics
on the landing pages), with the identity/privacy model, ingestion architecture, and an event
taxonomy that supports campaign analytics (UTM-tagged visit → connect → active usage) all
decided — so implementation can run as a normal superpowers plan session with nothing left to
decide. Implementation itself is NOT part of this map; connecting PostHog's official MCP to
Claude IS (a task ticket).

## Notes

- This is telemetry INTO PostHog for the operator — NOT a user-facing connector adapter
  (despite the `feature/posthog-adapter` branch name). PostHog ships its own MCP
  (mcp.posthog.com); we consume it, we don't wrap it.
- Decided at charting (2026-07-19):
  - **EU cloud** (eu.posthog.com), **new project** alongside WakePins.
  - Existing Slack reporting (`report.py`, `scripts/hourly_digest.py`) and the SQLite
    metrics **stay as a backup** — nothing is retired by this effort. New telemetry flows
    to PostHog, **forward-only** (no historical backfill).
  - **Web analytics is in scope**: posthog-js on the landing pages; the goal is campaign
    analytics — how campaigns drive connects and usage.
- Current telemetry surface (facts, for orientation): ~50 structured JSON log events to
  stdout (`mcp-response` carries account/tool/status/ttfb_ms/total_ms/bytes), SQLite
  `tool_usage` (adapter, account_key, tool, calls, last_used), the daily Slack user report
  (`report.py`), and the hourly log-derived health digest (`scripts/hourly_digest.py`,
  standalone in GitHub Actions).
- CLAUDE.md invariants apply throughout: secrets never logged, logs carry at most an
  8-char hash prefix, `account_key` is a lowercased email (PII!), no plain-text stderr,
  single-node process-local state, dependency-light ethos (`backup.py`'s dependency-free
  SigV4 signer is the precedent to match).
- Skills to consult per ticket: `/research` (ticket 01), `/grilling` + `/domain-modeling`
  (tickets 03, 04, 05, 07).

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [Create the EU PostHog project + keys](issues/02-create-posthog-project.md) — project
  created by the operator (2026-07-19); public `phc_` ingestion token handed over, parked
  for a Railway env var at implementation time; personal-API-key question deferred to the
  MCP ticket (OAuth may make it moot).
- [PostHog platform facts](issues/01-posthog-platform-research.md) — EU capture at eu.i.posthog.com (`/i/v0/e/`, `/batch/` <20MB, no rate limits, `phc_` key in body — httpx-direct beats the thread-based SDK); Logs is beta via OTLP (`/i/v1/logs`, Railway has no drains); ~10 users ≈ $0/mo inside the 1M free tier (identified ~5x anonymous price); posthog-js auto-UTM + cookieless `on_reject`; distinct_id must be a hash of `adapter:email` (PII); alerts → Slack native; MCP at mcp.posthog.com/mcp is EU-auto-routed with OAuth. Full asset: [assets/posthog-platform-research.md](assets/posthog-platform-research.md).
- [Connect PostHog MCP to Claude](issues/06-connect-posthog-mcp.md) — done via the
  first-party claude.ai connector (OAuth, no personal key); verified live against project
  MissingMCP.com (id 227772) on eu.posthog.com; exposes HogQL, insights, dashboards and a
  built-in MCP-analytics query family — plus a lead for ticket 05: emit PostHog's
  canonical `$mcp_*` events and that analytics UI works out of the box.
- [Identity & privacy model](issues/03-identity-privacy-model.md) — distinct_id = plain
  normalized email (person = human, adapter = event property); server-side `$identify`
  stitching via the posthog cookie on successful authorize (cross-device loss accepted);
  egress rule "identity + metadata yes, content never" (raw IP discarded, autocapture
  only on marketing pages); person deletion joins the revoke path.
- [Ingestion architecture](issues/04-ingestion-architecture.md) — in-process via the
  official `posthog` Python SDK (operator's call: less owned code + free exception
  autocapture, threads/4 deps accepted); logs tee'd to PostHog Logs via app-side OTLP
  (stdout→Railway stays the durable archive, hourly digest unchanged); everything
  env-gated (`POSTHOG_*`) and fire-and-forget à la backup.py.
- [Event taxonomy & campaign analytics model](issues/05-event-taxonomy.md) — canonical
  `$mcp_tool_call`/`$mcp_initialize`/`$mcp_tools_list` per request (built-in MCP
  analytics lights up); lean server set = connect funnel + conversions
  (`account_connected` new|returning is the campaign outcome; ops stays in logs);
  posthog-js defaults on marketing pages; canonical funnel $pageview→authorize→
  account_connected→first $mcp_tool_call; UTM discipline = utm_source+utm_campaign
  required on every shared link.

## Not yet specified

- The PostHog dashboard/insight/alert set to build once data flows — depends on the event
  taxonomy; likely graduates out of ticket 05 or lands in the spec.
- Whether PostHog alerts eventually take over the hourly digest's anomaly/heartbeat role —
  revisit once both run side by side.

## Out of scope

- Agents over the data in PostHog — a future effort; makes sense only once data flows.
- Retiring the Slack reports / SQLite metrics — explicitly kept as backup (operator
  decision, 2026-07-19).
- Implementation of the telemetry itself (separate superpowers plan session — the
  destination is the spec).
