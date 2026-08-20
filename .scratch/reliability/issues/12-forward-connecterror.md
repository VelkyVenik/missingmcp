# 12 — mcp-forward-error ConnectError on worker forwards (463/7d, growing)

Type: research
Status: resolved (2026-08-19 — mechanism established; fix graduated to
[14](14-fix-stale-listener-port-reuse.md), gateway-fault split to
[15](15-stream-teardown-asgi-escape.md))

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
- 2026-08-19 (operator asked "do we have too few workers?" — quick log
  check): **972 `worker-evicted` events in 24 h, 0 `worker-cap-all-busy`** —
  the gateway runs pinned at the `MAX_WORKERS` cap essentially all day,
  killing an idle worker ~1000×/day to make room, but an idle victim always
  exists (nobody is refused for capacity). New lead hypothesis: eviction
  churn IS the ConnectError mechanism — a just-SIGTERMed worker can still
  answer the `ensure_worker` healthz probe while its listener is closing, so
  the follow-up forward hits a dead port. Question 1's timing correlation
  should test eviction (not just reap) adjacency first. Fix direction gains a
  cheap lever: raise `MAX_WORKERS` (check service RAM headroom first — each
  worker is a full Python process) alongside the proxy-side race fix.

## Resolution (2026-08-19)

Mechanism established with high confidence: **stale-listener false-healthy on
immediate port reuse** — not a reap/evict race against an in-flight request
(the inflight accounting is airtight: no await between the probe release in
`ensure_worker` and `request_started`, and every kill path skips
`inflight > 0`).

The actual sequence:

1. `_alloc_port` hands out the **lowest** free port, so a fresh spawn
   typically inherits the port of the worker terminated moments earlier —
   usually the eviction victim of that very spawn's `_enforce_cap` (972
   evictions/24 h; the pool sits pinned at `MAX_WORKERS` all day).
2. `_terminate` is fire-and-forget SIGTERM; uvicorn shuts down gracefully and
   its listener keeps accepting for a few hundred ms — it even answers
   `/healthz 200` while dying.
3. `_wait_healthy` probes the **port**, not the process: the dying occupant
   answers within ~6 ms, and the half-booted new worker is registered as
   started.
4. The forward connects a few ms later — old listener now closed, new process
   not yet bound (a real bind takes ~1.3 s) → `ConnectError` → 502.
5. The client's retry probes the registered handle, finds it unhealthy,
   **kills the innocent still-booting process**, respawns, and succeeds
   ~1.5–2 s after the first attempt.

Evidence (Railway/PostHog logs, 24–48 h windows, accounts masked):

- **Sample timeline** (account sce\*\*\*, 20:18:36): `worker-spawn` port 9011
  → `worker-started ms=6` (impossible — real starts take ~1.3 s) →
  `mcp-forward-error ConnectError` 10 ms later → client retry 260 ms later →
  replace → second spawn on 9011 → `worker-started ms=1297` → `initialize`
  200. User-facing outage: ~2 s, self-healed.
- **Bimodal start times** (latest 200 `worker-started`): 187× 1000–2000 ms,
  2× ≥2000 ms, **11× ≤50 ms, nothing between 50–1000 ms**. The ≤50 ms cluster
  is the bug; scaled to daily spawn volume it matches the ~100
  ConnectErrors/day.
- Handshake dominance (`initialize`, `server/discover`) follows directly:
  ConnectError hits the request that *triggers a spawn*, and spawns are
  triggered by fresh sessions.

**Impact (Q2):** transient — one 502 on a handshake, session established
~2 s late on the client's automatic retry; plus one wasted spawn and one
killed innocent process per hit (extra churn).

**gateway-fault is NOT the same root cause:** 145 ASGI-exception rows/24 h
decompose as ~143 `httpx.RemoteProtocolError: incomplete chunked read`
(ticket 10's teardown class escaping through starlette's collapsing task
group — the `stream()` catch fired exactly **once** in 24 h) and ≤2 others.
Split to [15](15-stream-teardown-asgi-escape.md).

**Fix direction (Q3)** — graduated to
[14](14-fix-stale-listener-port-reuse.md): (a) port hygiene — round-robin
allocation plus a cooldown that keeps a freed port unavailable until its
previous process is observed dead (kills the class at the source); (b)
optional belt-and-braces — one proxy-side forward retry re-running
`ensure_worker` on `ConnectError` (removes the user-visible 502 for any
residual transient); (c) optional lever — raise `MAX_WORKERS` (RAM headroom
exists: 1.22 GB max at cap 10) to cut the 972 evictions/day that open the
window.
