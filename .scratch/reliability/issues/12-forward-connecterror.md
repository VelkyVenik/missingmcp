# 12 — mcp-forward-error ConnectError on worker forwards (463/7d, growing)

Type: research
Status: open

Surfaced by [06](06-post-fix-verification.md)'s 2026-08-12..19 measurement:
`mcp-forward-error` on garmin worker forwards — **ConnectError 428 +
ReadError 35 across 214 distinct accounts**, growing 27→109/day, dominated by
handshake methods (`server/discover` 286 — a non-standard method newer
clients probe — and `initialize` 150). The gateway fails to reach a worker it
just validated (`ensure_worker` returned a port after a `/healthz` answer),
and the client receives a **502** — plausibly user-facing when it hits
`initialize`. Post-#18 this is the main remaining pager feeder.

## Question

Establish the mechanism and impact:

1. **Timing correlation** — do the ConnectErrors cluster right after
   `worker-reaped`/`worker-evicted` events for the same account (a race:
   request in flight while the reaper kills the worker between `ensure_worker`
   and the forward — the inflight guard covers the STREAM, but is the window
   between `ensure_worker` returning and `request_started` being called
   covered?), or after worker self-exits?
2. **User impact** — the client receives a 502 `bad_gateway`; do affected
   accounts retry successfully right after (transient) or repeatedly fail?
3. **Fix direction** — e.g. move `manager.request_started(key)` before the
   awaited `client.send` (it already is — verify the actual gap), retry the
   forward once on ConnectError re-running `ensure_worker`, or widen the
   inflight hold from `ensure_worker`'s return through the send.

Read `proxy.handle_mcp`'s ordering around `ensure_worker` →
`request_started` → `client.send`, and correlate logs per masked account.
Aggregates only — public repo.

## Comments

- 2026-08-19 (from the triage dry-run): 107 `mcp-forward-error` rows in the
  last 24 h, dominated by `initialize` / `server/discover`. The 155-row
  `gateway-fault` class (uvicorn "Exception in ASGI application" with httpx
  ConnectError tracebacks) is plausibly the SAME root cause seen from the
  gateway's own request path — the daily triage analysis proposed confirming
  that correlation first. This is now the main remaining pager-feeder class;
  research priority up.
