# Garmin Gateway

A multi-user, OAuth 2.1–protected remote MCP gateway. This glossary pins the
domain language so the same word means the same thing in code, docs, and
conversation.

## Language

### Connectors & adapters

**Connector**:
What a person adds in their MCP client — one URL, one sign-in, one entry in
Claude's Settings → Connectors. Every connector is served by exactly one adapter.
_Avoid_: adapter (that's the gateway's view), plugin, integration

**Adapter**:
The gateway-side implementation of one connector — its login shape, its
credential blob, its forward strategy. Named in `(adapter, account_key)` and in
the URL `/<adapter>/mcp`.
_Avoid_: connector (that's the person's view), provider, integration

**Upstream**:
The third-party service an adapter connects to (currently Garmin) — the system that
owns the data and authenticates the account. Never the gateway itself.
_Avoid_: provider, backend, vendor

**Forward strategy**:
How an adapter serves `/<adapter>/mcp` — **worker** (proxied to a per-account
subprocess), **remote** (forwarded to a hosted upstream MCP), or **local** (served
inside the gateway process).
_Avoid_: transport, mode, backend

**Worker**:
A per-account subprocess running an unmodified upstream MCP server, reachable
only by the gateway. Only the worker strategy has them.
_Avoid_: instance, child process, server

**Retired adapter**:
An adapter deliberately taken out of service and named on the explicit
`RETIRED_ADAPTERS` list — e.g. `rohlik`, retired 2026-07-06. Its rows are purged
from every table. **Not** the same as "absent from the registry": absence is
config-dependent, and treating it as retirement deletes live data (ADR-0001).
_Avoid_: dead adapter, dropped adapter, disabled adapter

### OAuth & connection lifecycle

**Account**:
A person's connection to one upstream service, keyed by `(adapter, account_key)`
where `account_key` is their lowercased login email. Holds the encrypted
credential blob.
_Avoid_: user, customer, login (those blur account vs. device vs. person).

**Active account**:
An account that invoked a tool inside a given window. The handshake and
discovery calls an MCP client makes on its own (`initialize`, `tools/list`, …)
are **protocol traffic**, not activity — an account doing only those is
connected, not active (ADR-0002).
_Avoid_: active user (blurs account vs. person), usage, engaged

**Device**:
One issued access token — a single Claude client (mobile/desktop/web) connected
to an account. Revoking a device logs it out without touching the account.
_Avoid_: session, token (when you mean the connection rather than the string).

**OAuth client**:
A DCR (Dynamic Client Registration) record in `oauth_clients`. Claude registers a
fresh one on every connection attempt; it is only ever consulted during the OAuth
flow (authorize + `/token`) and is dead weight once its flow ends.

**Orphan client**:
An OAuth client with zero access tokens — a registration whose OAuth flow never
completed (user abandoned it, or Claude retried). The one thing that accumulates
without bound. This is what "unconfigured client" means.
_Avoid_: unconfigured, dangling.

**Stale token / device** (defined, out of current cleanup scope):
An access token whose `last_used` is older than a chosen threshold. This is what
"unused token" means.

**Orphan account** (defined, out of current cleanup scope):
An account row with no live access token — stored credentials for someone with no
connected device.
