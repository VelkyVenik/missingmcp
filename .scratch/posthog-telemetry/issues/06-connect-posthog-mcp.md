# 06 — Connect PostHog MCP to Claude

Type: task
Status: resolved
Blocked by: 02

## Question

Connect PostHog's official MCP server (mcp.posthog.com) to the operator's Claude (the
claude.ai PostHog connector already exists in the environment, unauthenticated) using the
personal API key from ticket 02; verify it can answer usage questions against the
missingmcp project (EU region). Record the working setup (connector config, which queries
work) — these facts land in the spec's "operating it" section.

## Answer

Connected and verified, 2026-07-19:

- **Auth**: the first-party claude.ai PostHog connector via OAuth — no `phx_` personal API
  key needed (closes the question ticket 02 deferred here). Setup: authenticate the
  connector on claude.ai (Settings → Connectors → PostHog), then in a Claude Code session
  run `/mcp reconnect all` to pick it up (the server name `claude.ai PostHog` contains a
  space, so `reconnect all` is the reliable form).
- **Verified environment**: active project **MissingMCP.com** (id 227772), org
  VaclavSlajs, base URL **eu.posthog.com** — the EU-cloud charting decision reads back
  correctly. Query path proven with a live `read-data-schema` call (returns the default
  event taxonomy on the fresh project).
- **What it exposes**: one `exec` meta-tool wrapping the full PostHog tool catalog —
  HogQL (`execute-sql`), typed insight queries (trends/funnel/retention/paths/web
  analytics), dashboards, error tracking, logs, **and a dedicated MCP-analytics family**
  (`query-mcp-tool-stats`, `query-mcp-harness-breakdown`, `query-mcp-tool-failures`, …).
- **Discovery for ticket 05**: PostHog ships canonical MCP event definitions
  (`$mcp_tool_call`, `$mcp_initialize`, `$mcp_tools_list`, `$mcp_custom`, …) that back
  those query-mcp-* tools — see the comment left on ticket 05.
