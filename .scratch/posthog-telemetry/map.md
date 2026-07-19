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

## Not yet specified

- The PostHog dashboard/insight/alert set to build once data flows — depends on the event
  taxonomy; likely graduates out of ticket 05 or lands in the spec.
- Concrete UTM conventions for marketing campaigns (LinkedIn etc.) — may graduate within
  ticket 05.
- Whether PostHog alerts eventually take over the hourly digest's anomaly/heartbeat role —
  revisit once both run side by side.

## Out of scope

- Agents over the data in PostHog — a future effort; makes sense only once data flows.
- Retiring the Slack reports / SQLite metrics — explicitly kept as backup (operator
  decision, 2026-07-19).
- Implementation of the telemetry itself (separate superpowers plan session — the
  destination is the spec).
