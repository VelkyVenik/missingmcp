# MissingMCP Gateway

A multi-user, OAuth 2.1–protected remote MCP gateway. This glossary pins the
domain language so the same word means the same thing in code, docs, and
conversation.

## Language

### OAuth & connection lifecycle

**Account**:
A person's connection to one upstream service, keyed by `(adapter, account_key)`
where `account_key` is their lowercased login email. Holds the encrypted
credential blob.
_Avoid_: user, customer, login (those blur account vs. device vs. person).

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

**Dead-adapter row**:
Any row keyed to an `adapter` no longer present in the registry
(`adapters.build_adapters`) — e.g. `rohlik`, retired 2026-07-06. Can never become
live again.

**Stale token / device** (defined, out of current cleanup scope):
An access token whose `last_used` is older than a chosen threshold. This is what
"unused token" means.

**Orphan account** (defined, out of current cleanup scope):
An account row with no live access token — stored credentials for someone with no
connected device.
