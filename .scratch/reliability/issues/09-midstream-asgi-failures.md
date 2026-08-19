# 09 — Mid-stream failures on proxied worker responses (growing since 2026-08-03)

Type: research
Status: claimed

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
