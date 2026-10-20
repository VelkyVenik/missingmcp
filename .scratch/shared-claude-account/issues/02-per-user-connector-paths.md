# 02 — Per-user connector paths with matching protected-resource metadata

Type: task
Status: open (todo — later)
Blocked by: [01](01-document-shared-claude-account-trick.md) (ship the docs first)

## Context

The `?u=` trick from [01](01-document-shared-claude-account-trick.md) works
today, but `/.well-known/oauth-protected-resource/garmin/mcp` returns the
resource **without** `?u=…`. Clients accept that now; it is not RFC 9728
strict (resource ≠ the URL the client was configured with) and a query param
is not discoverable.

## Task

Proper per-user URLs such as `/garmin/mcp/<tag>`:

- route `/{adapter}/mcp/<tag>` to the same MCP handler (tag carries no
  identity; the account is still decided by the Garmin sign-in);
- serve `/.well-known/oauth-protected-resource/{adapter}/mcp/<tag>` with
  `resource` = the exact tagged URL, and point the 401 `WWW-Authenticate`
  `resource_metadata` at it;
- keep `?u=` working (already-configured connectors must not break);
- update the website copy from 01 to the path form once shipped.

Validate `<tag>` (short, `[a-z0-9-]`), and keep telemetry volume unchanged
(no per-tag events).
