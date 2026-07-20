# 07 — Write the design spec

Type: grilling
Status: resolved
Blocked by: 03, 04, 05

## Question

Assemble the destination: `docs/superpowers/specs/<date>-posthog-telemetry-design.md`
(following the shape of `2026-07-06-whoop-adapter-design.md`) — identity/privacy model
(03), ingestion architecture (04), event taxonomy + campaign funnel (05), the web snippet
plan, rollout/verification steps, and what stays with Slack/SQLite as backup. Reviewed
with the operator; the map closes when the spec is agreed.

## Answer

Written, reviewed and approved by the operator (2026-07-20):
[docs/superpowers/specs/2026-07-20-posthog-telemetry-design.md](../../../docs/superpowers/specs/2026-07-20-posthog-telemetry-design.md)
— identity/privacy model, SDK ingestion, OTLP log tee, event taxonomy incl. the
canonical `$mcp_*` mapping, web snippet plan, UTM discipline, rollout/verification and
testing approach. Two flagged implementation-time verification points: exact `$mcp_*`
property keys (against `@posthog/mcp` source) and the cookieless consent UX. With this
the map's destination is reached — nothing left to decide before implementation runs as
a normal superpowers plan session.
