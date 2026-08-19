# 10 — Quiet the routine stream-teardown noise (80 % of current error volume)

Type: task
Status: claimed
Blocked by: 09

Execution ticket (hybrid map — mechanism established by
[09](09-midstream-asgi-failures.md)'s answer).

## Question

A routine MCP session teardown currently costs two ERROR rows: the gateway
lets `httpx.RemoteProtocolError` ("peer closed connection without sending
complete message body") propagate out of `proxy.py::stream()` into a full
uvicorn "Exception in ASGI application" traceback, and the worker's mirror
line "ASGI callable returned without completing response" is elevated to
error by the `workers._WORKER_ERROR` pump filter. ~290 rows/day, ~80 % of all
error volume, zero demonstrated user impact.

Implement:

1. In `proxy.stream()`, catch the teardown family
   (`httpx.RemoteProtocolError`, `httpx.ReadError`, and cancellation-adjacent
   stream aborts) around `aiter_raw()`: stop iterating, run the existing
   `finally` bookkeeping unchanged (`mcp-response` still logs), and emit ONE
   structured **warn** event — `mcp-stream-interrupted` (adapter, account,
   tool/method, error type, bytes sent). Never propagate into the ASGI stack;
   never swallow silently — a POST-side surge would be user-facing and must
   stay visible to triage.
2. In `workers._pump_worker_output`, demote the exact line
   "ASGI callable returned without completing response" to info — a narrow,
   deliberate exception to the deliberately-loose error filter (comment why).
3. Tests: fake worker cutting a chunked stream mid-body → 200 streamed
   partial, `mcp-stream-interrupted` logged at warn, no exception escapes;
   pump demotion covered in test_workers.

Log-schema note: new event name added (`mcp-stream-interrupted`), nothing
renamed; the hourly digest counts error/critical rows only, so warn rows
stop feeding the pager by construction. Feature branch + PR.

## Comments

- 2026-08-19: implemented on `fix/quiet-stream-teardown`, **PR #18 open**
  (https://github.com/VelkyVenik/missingmcp/pull/18), awaiting CodeRabbit +
  merge. Suite 375 passed. Resolves on merge + deploy; expected effect is a
  ~80 % drop in error volume — verify in the next digest hours after deploy.
