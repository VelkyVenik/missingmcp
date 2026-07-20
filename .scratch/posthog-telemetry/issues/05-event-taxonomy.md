# 05 — Event taxonomy & campaign analytics model

Type: grilling
Status: resolved
Blocked by: 01, 03

## Question

Which events (server + web), with which properties and names, so that the campaign
analytics the operator wants — how campaigns drive connects and usage — falls out of the
data?

1. **Server events**: which of the ~50 structured log events graduate to PostHog events
   (tool call = `mcp-response`, the sign-in funnel, worker health, subscribe/suggest…),
   and at what granularity (every tool call vs. aggregates).
2. **Web events**: pageviews/autocapture on the landing pages, UTM capture, the connect
   funnel (visit → OAuth start → account created → first tool call).
3. **Naming/property conventions** the spec locks down — stable-schema discipline,
   mirroring the "log event names are a stable schema" invariant.

## Comments

- 2026-07-19 (while resolving ticket 06): PostHog ships **canonical MCP analytics event
  definitions** — `$mcp_tool_call` (tool name, duration, error state, agent intent),
  `$mcp_initialize` (client name/version handshake), `$mcp_tools_list`, `$mcp_custom` —
  normally emitted by the `@posthog/mcp` TypeScript SDK, and a matching family of
  built-in query tools (`query-mcp-tool-stats`, `query-mcp-harness-breakdown`,
  `query-mcp-tool-failures`, `query-mcp-tool-top-users`, …). Our gateway is Python and
  hand-rolled, but if it emits these canonical names/properties for `/​<adapter>/mcp`
  traffic, PostHog's MCP analytics lights up for free. Weigh this against inventing our
  own event names when resolving this ticket.

## Answer

Decided with the operator, 2026-07-20:

1. **Canonical `$mcp_*` events for MCP traffic**: the gateway emits `$mcp_tool_call` for
   every forward (properties from the `mcp-response` record — tool, status, ttfb/total
   ms, bytes — plus our `adapter`), and `$mcp_initialize` / `$mcp_tools_list` for
   handshake/list requests, so PostHog's built-in MCP analytics (`query-mcp-*`) works
   out of the box. Granularity: every call is an event (~20–30k/mo, no volume concern).
2. **Server events = product/campaign analytics only** — ops stays in the OTLP log
   channel. The set: connect funnel `login_succeeded` / `login_failed` /
   `mfa_challenged` / `account_connected` (property `new|returning` — the key campaign
   outcome) / `account_revoked`; conversions `subscribe` (anonymous web person, no email
   property) and `suggest`; plus the `$identify` stitch from ticket 03. snake_case
   names; event names/properties are a stable schema (same discipline as the log-event
   invariant).
3. **Web via posthog-js defaults on marketing pages** (pageview/pageleave/autocapture,
   auto-UTM, cookieless `on_reject`), explicit pageview only on OAuth pages. Canonical
   campaign funnel: `$pageview` (UTM) → authorize-form pageview → `account_connected`
   (new) → first `$mcp_tool_call`. **Mini UTM discipline**: every shared link carries
   `utm_source` + `utm_campaign` (`utm_medium` optional); the spec holds the agreed
   `utm_source` value table (linkedin / twitter / reddit / direct / …).
