# Evaluate the new MCP specification — what would it bring us?

Type: research
Status: resolved (2026-08-20 — operator accepted "wait, adopt nothing now"; watch items below stand, re-check ~quarterly or on trigger)

Operator ask (2026-08-19): "zvážit novou specifikaci MCP — co by nám přinesla."

## Question

The gateway advertises `protocolVersion: 2025-06-18` (server cards in
`app.py`) and both server implementations predate the current spec: the
garmin worker is the unmodified `garmin_mcp` pinned to the mcp **1.x** SDK
(the 2.0.0 SDK — released 2026-07-28, presumably tracking a newer spec
revision — removed `mcp.server.fastmcp` and is pinned OUT in the Dockerfile
after the 2026-07-31 outage), and `/whoop/mcp` is a hand-rolled stateless
JSON-RPC server (`adapters/whoop/mcp.py`).

Research, against the official spec changelog and the mcp 2.x SDK release
notes:

1. **What changed** since revision 2025-06-18 — protocol revisions, transport
   semantics (session lifecycle / listen-stream teardown — directly relevant
   to the `mcp-stream-interrupted` class from reliability ticket 09), auth
   (RFC 9728 flow changes?), new capabilities (tasks, elicitation, …).
2. **What adoption would bring us** concretely: user-visible features in
   Claude, protocol-level fixes for the stream-teardown noise, discovery/
   registry benefits, anything the current pins block us from.
3. **What it would cost**: the garmin worker upgrade depends on
   `Taxuspt/garmin_mcp` adopting the 2.x SDK (we never modify it — black
   box); the whoop server and the proxy/discovery surface are ours to change;
   client compatibility (which protocolVersion do Claude clients negotiate
   today?).
4. **Recommendation**: adopt now / wait for the worker upstream / partial
   adoption (whoop only) — with the reliability map's quiet-Slack goal in
   mind (don't destabilize what just got quiet).

Output: findings + recommendation appended here under `## Answer`; if it
grows into real work, chart it as its own wayfinder effort.

## Answer (2026-08-20)

**Recommendation up front: wait — adopt nothing now.** Two spec revisions
shipped since ours, but every client that talks to us today still speaks the
old ("legacy") era, the garmin worker is gated on an upstream that has
deliberately pinned itself to the 1.x SDK, and the one protocol change that
would have helped (session/stream teardown) only pays off when both sides
move. Adoption today is cost and deploy risk on the live MCP path with zero
present-day user value — exactly what the reliability map says not to do.
Three watch items below; none is urgent before ~mid-2027.

### 1. What changed since 2025-06-18

Two revisions: **2025-11-25** and **2026-07-28** (current latest; locked as
RC 2026-05-21, final 2026-07-28 —
[release post](https://blog.modelcontextprotocol.io/posts/2026-07-28/)).

**2025-11-25**
([changelog](https://modelcontextprotocol.io/specification/2025-11-25/changelog))
— incremental, backwards-compatible:

- Auth: OIDC Discovery as an additional AS-discovery mechanism (PR #797);
  **OAuth Client ID Metadata Documents (CIMD) recommended for client
  registration** (SEP-991); incremental scope consent via `WWW-Authenticate`
  (SEP-835); RFC 9728 alignment — `WWW-Authenticate` optional with
  `.well-known` fallback (SEP-985; we already serve the `.well-known` route).
- Metadata/UX: icons on tools/resources/prompts (SEP-973); tool-name
  guidance (SEP-986); JSON Schema 2020-12 as default dialect (SEP-1613).
- Capabilities: URL-mode elicitation (SEP-1036), richer enums (SEP-1330),
  sampling tool-calls (SEP-1577), **experimental tasks** (SEP-1686).
- Transport: **SEP-1699 — servers may disconnect SSE streams at will**
  (polling model legitimized; clarified in #1847). Invalid `Origin` ⇒ 403
  (PR #1439). Input-validation failures are tool errors, not protocol
  errors (SEP-1303; `adapters/whoop/mcp.py::_tool_error` already does this).

**2026-07-28**
([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog))
— the big, breaking one ("modern" era):

- **Stateless core**: protocol sessions and `Mcp-Session-Id` removed
  (SEP-2567); the `initialize`/`initialized` handshake removed — every
  request carries protocolVersion/capabilities/clientInfo in `_meta`;
  version mismatch ⇒ `UnsupportedProtocolVersionError` (SEP-2575). New
  mandatory `server/discover` RPC.
- **Listen stream gone**: the HTTP GET endpoint is replaced by an opt-in
  `subscriptions/listen` POST stream; **SSE resumability/`Last-Event-ID`
  removed** — a broken stream just means re-issue the request (SEP-2575).
- `Mcp-Method`/`Mcp-Name` headers required on POSTs — gateways can route
  without parsing bodies (SEP-2243). `ttlMs`/`cacheScope` caching fields
  required on list results (SEP-2549). Tasks redesigned as an extension;
  MRTR pattern replaces server-initiated requests; MCP Apps extension
  (SEP-2663, SEP-2322).
- Auth: **DCR (RFC 7591) deprecated in favor of CIMD** (PR #2858, 12-month
  minimum deprecation window per the new feature-lifecycle policy,
  SEP-2596); AS SHOULD send RFC 9207 `iss` and clients MUST validate it
  (SEP-2468); DCR clients must send `application_type` (SEP-837);
  credentials keyed by issuer (SEP-2352). Roots/Sampling/Logging deprecated
  (SEP-2577).

**Compatibility semantics**
([versioning page](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)):
legacy = handshake era (≤2025-11-25), modern = per-request `_meta` era
(≥2026-07-28). The compatibility matrix puts the fallback burden on
*clients*: a dual-era client probes, gets a `400` without a modern error
body from a legacy server like ours, and **falls back to `initialize` — so
"Legacy server ← Dual-era client" works indefinitely.** Only a
modern-*only* client would fail against us, and none exist in our traffic
(next section).

### 2. What do clients actually speak today (empirical)

PostHog `$mcp_initialize` census, last 14 days (~30k events): every client
sends the legacy `initialize` handshake — Anthropic/ClaudeAI ~19.3k,
claude-code 2.1.x ~6.7k, openai-mcp/Codex ~2.9k, Anthropic/Toolbox 307,
Anthropic/API 254, others small. **Zero modern-era traffic.** Our whoop
server answers `2025-06-18` from `PROTOCOL_VERSIONS`
(`adapters/whoop/mcp.py:13,130`) and all of them accept it — interop is
proven daily in production, not just promised by the matrix.

### 3. What adoption would bring

- **User-visible features: effectively nothing.** Both our servers are
  read-only tool servers; tasks/elicitation/MRTR/Apps don't apply. Icons
  (2025-11-25) and `ttlMs` caching (2026-07-28) are cosmetic/marginal.
- **The stream-teardown class (reliability 09/10/15)**: the modern era
  removes the thing that generates it — no sessions, no GET listen stream,
  no resumability; SEP-1699 even blesses at-will disconnects in the legacy
  era. So the noise is *architecturally* eliminated by 2026-07-28 — but
  only once **both** the worker (upstream-gated) and the clients (all
  legacy today) move. Meanwhile ticket 10 already demoted it to one warn
  event, and whoop never had a listen stream (local strategy 405s non-POST,
  `proxy.py:145-147`). **No practical noise gain available to us now.**
- **Auth/CIMD is the strategically relevant piece**: when Claude clients
  start presenting URL client IDs, our DCR-only `/oauth/register` flow
  must learn to accept CIMD or logins break — and CIMD would incidentally
  dissolve the orphan-client-registration churn (no more per-client DB
  rows to sweep). Observed clients still register via DCR today, and the
  deprecation window guarantees DCR acceptance through ~mid-2027.

### 4. What it would cost

- **garmin**: nothing we can do. The proxy forwards JSON-RPC bodies
  verbatim (`proxy.py:228-229`), so the worker's protocol era is entirely
  upstream's. Upstream pinned itself to `mcp>=1.28.1,<2`
  ([pyproject](https://github.com/Taxuspt/garmin_mcp/blob/main/pyproject.toml),
  cap added in [#227](https://github.com/Taxuspt/garmin_mcp/pull/227)
  after [#226](https://github.com/Taxuspt/garmin_mcp/issues/226), same
  reasoning as our Dockerfile `"mcp<2"` pin) — no open 2.x-migration
  issue/PR as of 2026-08-20, though the repo is otherwise active. The
  python-sdk 1.x line (1.29.0, 2026-07-28) is in maintenance mode,
  security fixes only
  ([releases](https://github.com/modelcontextprotocol/python-sdk/releases))
  — a slow-burning clock on the whole 1.x ecosystem, not an emergency.
- **whoop → advertise 2025-11-25**: one line (`PROTOCOL_VERSIONS`) plus
  optional icons; near-zero risk, near-zero benefit.
- **whoop → dual-era 2026-07-28**: moderate, real work — `server/discover`,
  per-request `_meta` handling, `Mcp-Method`/`Mcp-Name` validation,
  `resultType` on results, `ttlMs`/`cacheScope` — on the live MCP path,
  for zero present-day clients. (The server *is* already stateless, so the
  eventual migration is unusually cheap for us — just premature.)
- **oauth surface**: CIMD acceptance is a contained, additive feature in
  `oauth.py` when its time comes; RFC 9207 `iss` is a SHOULD we can add
  cheaply in the same effort.

### Watch items (re-check ~quarterly, or on trigger)

1. **Client era**: re-run the `$mcp_initialize` client census; trigger =
   Anthropic announcing 2026-07-28 support / modern-era requests appearing
   (they'd surface as 400s or `-32601` on our servers).
2. **Upstream**: a 2.x-migration issue/PR appearing in `Taxuspt/garmin_mcp`
   — then re-open this as a wayfinder effort (worker + Dockerfile pin +
   regenerate tool docs).
3. **CIMD adoption by Claude clients** — then chart the oauth effort
   (CIMD + `iss`), which also closes the orphan-client class for good.
