# 05 — Event taxonomy & campaign analytics model

Type: grilling
Status: open
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
