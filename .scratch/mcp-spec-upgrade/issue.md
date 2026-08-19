# Evaluate the new MCP specification — what would it bring us?

Type: research
Status: ready-for-agent

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
