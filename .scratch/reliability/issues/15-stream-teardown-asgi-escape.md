# 15 — Stream-teardown RemoteProtocolError still escapes as an ASGI error

Type: task
Status: open — likely MOOT, verify ~2026-08-22 and close if still clean

2026-08-20 post-deploy data (12.3 h since ticket 14's port-cooldown fix):
**0** ASGI escapes AND **0** `mcp-stream-interrupted` warns (baselines: ~143
escapes + ~1 warn per day). The whole "routine teardown" class appears to have
been ticket 12's bug seen from the other side: a request that connected to
the dying predecessor fast enough got ACCEPTED and was then cut mid-body when
the process exited — RemoteProtocolError "incomplete chunked read". With
ports no longer recycled under dying listeners, the class vanished. If a
couple more days stay at zero, close this without any code change (ticket
09/10's residual diagnosis gets corrected by this note).

Split from [12](12-forward-connecterror.md)'s resolution. Ticket 10 (PR #18)
demoted routine MCP session teardowns to one `mcp-stream-interrupted` warn by
catching `httpx.RemoteProtocolError` in `proxy.stream()` — but production
shows the catch fires almost never: in a 24 h window it caught **1**
teardown while **~143** `RemoteProtocolError: incomplete chunked read`
tracebacks still landed as error-level `Exception in ASGI application` rows
(uvicorn.error). This "gateway-fault" class is now the single largest
error-log feeder (~145 rows/day), it is NOT related to the ConnectError
mechanism, and it has no demonstrated user impact — pure pager noise.

## Question

Why does the exception bypass the generator's `except`, and where is the
right place to quiet it? The sampled traceback surfaces through
`starlette/responses.py stream_response` → `create_collapsing_task_group`
(`_utils.py:93 raise exc from ...`) — i.e. the client disconnects at the same
moment the upstream aborts (exactly what an MCP session teardown does), the
body task is being cancelled while `aiter_raw()` raises, and the collapsing
task group re-raises the httpx error OUTSIDE the generator. Verify that
reading of starlette's cancellation/exception collapse, then pick the fix:

- catch at the response layer (a small `StreamingResponse` subclass whose
  `stream_response` wraps the parent call and swallows/warn-logs this class),
- or demote the specific uvicorn.error record in `log.py`'s stdlib bridge
  (string-match "incomplete chunked read" — crude but zero-risk to the data
  path),
- or another mechanism the verification suggests.

Constraints: keep a POST-side surge visible to triage (same reasoning as
ticket 10 — warn, not silence); don't touch the stable `mcp-response` /
`mcp-stream-interrupted` event schema; the existing CutStream-style fake
worker test must keep passing, plus a new test reproducing the simultaneous
client-disconnect + upstream-abort race if feasible.
