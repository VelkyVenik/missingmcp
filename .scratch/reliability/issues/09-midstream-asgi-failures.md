# 09 — Mid-stream failures on proxied worker responses (growing since 2026-08-03)

Type: research
Status: resolved

## Question

A new error class dominates the post-fix log stream and is **growing**
(daily error/fatal rows 33 → 69 → 83 → 110 → 125 over 2026-08-01..05):
paired signatures of the gateway's uvicorn logging **"Exception in ASGI
application"** with an httpx traceback dying inside a *streamed response
read* (`httpx ... map_httpcore_exceptions ... _receive_response_body`,
112 rows), and the worker's uvicorn logging **"ASGI callable returned
without completing response"** (89 rows, spread across many accounts, peak
47/day on 08-05). It looks like proxied streams (SSE listen streams or
long tool-call responses on `/garmin/mcp`) being cut mid-flight — plausibly
`proxy.FORWARD_TIMEOUT_S` (30 s) applied as an httpx *read* timeout killing
an idle-but-healthy stream, or clients hanging up.

Establish:

1. What exactly dies — the GET listen stream, POST tool calls, or both?
   (Correlate with `mcp-response`/`mcp-timeout` events and methods.)
2. What changed around 2026-08-03 — client behaviour, usage growth, or a
   gateway change? (No gateway deploy maps to that date.)
3. User impact — do clients recover silently (reconnect) or does a user-facing
   failure result? Is any of it behind the remaining loud digest hours?
4. Fix direction — e.g. exempt streamed reads from the 30 s read timeout
   (keep a connect/total budget), or pass disconnects through quietly and
   demote the log severity if it's routine client hang-up noise.

Baseline material: the 2026-08-06 breakdown in this session (485 error rows
2026-08-01..06; stale-token class collapsed to 11, garminconnect portal-login
failures ~77, this class ~200 and rising). Ticket
[06](06-post-fix-verification.md)'s scheduled run (2026-08-07) will quantify
it independently — read its asset before starting. Aggregates only — public
repo.

## Comments

- 2026-08-19: the class **exploded** while unattended — 2026-08-06..19:
  1,913 gateway "Exception in ASGI application" + 1,868 worker "ASGI callable
  returned without completing response" = **~79 % of all 4,789 error rows**
  (~290/day). Traffic grew ~3× in the same window (~15k mcp-requests/day), so
  the class outgrew traffic. `mcp-timeout` = 0 and no 5xx — consistent with
  streams cut after headers, not hard request failures. Note: ticket 06's
  scheduled run never delivered (no branch/PR from the cloud routine), and
  PostHog Logs retention (~2 weeks) has aged out the original window.

## Answer (2026-08-19)

**Mechanism confirmed from full production tracebacks — it is NOT the 30 s
read-timeout.** Every sampled gateway traceback terminates in
`httpx.RemoteProtocolError: peer closed connection without sending complete
message body (incomplete chunked read)`, raised inside
`proxy.py::stream() → upstream.aiter_raw()` while Starlette is serving the
StreamingResponse. The **worker (peer) closes an in-flight chunked/SSE stream
without the terminating chunk** — the signature of the MCP streamable-http
session tearing down (client hangs up / DELETEs the session; the worker's
FastMCP server aborts its open GET listen stream). The worker's own uvicorn
logs the mirror line "ASGI callable returned without completing response"
(1,868×), and the gateway lets the exception propagate out of the generator,
so its uvicorn prints a full ERROR traceback (1,913×). Two error rows per
routine stream teardown.

Answers to the ticket's questions:

1. **What dies:** proxied streams after headers — dominated by GET listen
   streams (7,146 garmin GET requests in the window, same order as the 1,913
   exceptions; not every stream is torn down abruptly). Exact GET/POST
   attribution isn't derivable from the logs (no request id joins the
   traceback to an mcp-request row) — recorded as a known uncertainty.
2. **Why it grew:** no gateway deploy maps to the growth; traffic tripled and
   Claude clients hold/teardown listen streams per session — the class scales
   with sessions. Growth ≈ adoption, not a regression.
3. **User impact: none demonstrable.** `mcp-timeout` = 0, no 5xx, and the
   `finally` in `stream()` still logs `mcp-response` (status 200) for these
   requests. A torn-down listen stream is routine protocol behaviour the
   client re-opens. The damage is operator-side only: ~290 ERROR rows/day
   keeping the hourly digest loud.
4. **Fix direction** (graduated into ticket
   [10](10-quiet-stream-teardown.md)): catch the stream-teardown exception
   family in `proxy.stream()` and log ONE structured `warn` event
   (`mcp-stream-interrupted`: adapter, account, error type, bytes sent)
   instead of propagating; and demote the worker's mirror line in the
   `workers._WORKER_ERROR` pump filter. Keep it visible as `warn` — never
   swallowed silently — so a POST-side surge (which WOULD be user-facing)
   still shows up in triage.
