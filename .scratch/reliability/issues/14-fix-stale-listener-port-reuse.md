# 14 — Fix the stale-listener false-healthy on worker port reuse

Type: task
Status: shipped (PR #23, merge `ba4cfca`, deployed 2026-08-20 morning)

Items 1+2 shipped: round-robin `_alloc_port` + cooldown of terminated
workers' ports until their process is observed dead (SIGKILL escalation 5 s,
hard expiry 10 s), and one proxy-side `ConnectError` retry re-running
`ensure_worker` (warn event `mcp-forward-retry`). 7 new tests incl. the
dying-listener regression; full suite 396 passed. Item 3: operator set
`MAX_WORKERS=20` (2026-08-20) — not 30, since Railway bills by RAM used and
the cap directly scales the resident worker count; 20 halves the eviction
churn for ~+0.8 GB instead of ~+2 GB.

Success criterion to watch (daily triage will show it): `worker-started`
with `ms < 250` disappears; `mcp-forward-error ConnectError` drops to ~0
(residuals surface as the `mcp-forward-retry` warn instead of a user-facing
502).

## Verified (2026-08-20, 12.3 h post-deploy)

- `mcp-forward-error`: **0** (window baseline ~55). `mcp-forward-retry`: 0 —
  the root-cause fix alone did it; the belt-and-braces never fired.
- `worker-started`: 953; the only sub-250 ms start (7 ms, 07:05:21) was the
  OLD container still serving during the build. Since the new code took over:
  min ~790 ms, all legit.
- Error-level rows overall: 20 in 12.3 h vs ~200 in the same window before —
  all known routine classes (garmin-upstream login blocks, one bad password,
  CSRF noise, worker tool-call tracebacks).
- Side effect: the gateway-fault/teardown class ALSO went to zero — see
  [15](15-stream-teardown-asgi-escape.md), likely moot.
- `MAX_WORKERS` 10→20 barely moved evictions (774/12.3 h vs 849 in the same
  window yesterday, −9 %): daytime concurrency exceeds 20, the pool stays
  pinned. RAM avg went 0.63→1.22 GB. Evictions are harmless now, so the cap
  is purely a cold-start-latency vs RAM-cost dial — operator's call whether
  20 stays.

Graduated from [12](12-forward-connecterror.md), which established the
mechanism behind ~100 user-facing 502s/day: `_alloc_port` immediately reuses
the port of a just-terminated worker, the dying occupant's uvicorn still
answers `/healthz 200` for a few hundred ms, so `_wait_healthy` validates the
WRONG process and the follow-up forward hits a dead port (`ConnectError`).
The client's retry then kills the innocent still-booting worker and respawns
— every hit costs one 502 plus one wasted ~1.3 s spawn.

## Question

Implement, in order of leverage:

1. **Port hygiene (the actual fix):** stop handing a freed port to the next
   spawn while its previous owner may still be dying. Round-robin the
   allocation cursor across `worker_port_start..worker_port_end` instead of
   lowest-free-first, AND hold ports of terminated workers in a cooldown set
   until their process is observed dead (`proc.poll() is not None` — the
   reaper/terminate paths already hold the handles). Either alone probably
   suffices; together they kill the class. Success criterion:
   `worker-started` with `ms < 250` disappears from production
   (ticket 12 measured 11 such rows in the latest 200 starts).
2. **Optional belt-and-braces:** in `proxy.handle_mcp`, on
   `httpx.ConnectError` from `client.send`, re-run `ensure_worker` once and
   retry the forward before answering 502 — removes the user-visible error
   for any residual transient. Weigh against added latency on genuine
   failures.
3. **Optional lever (config, no code):** raise `MAX_WORKERS` on Railway to
   cut the 972 evictions/day that open the window (RAM headroom exists:
   1.22 GB max at cap 10; each worker ≈ 80–100 MB). Decide the value with the
   operator.

Constraints: `garmin_mcp` stays a black box (no worker-side changes);
`worker-started`/`worker-evicted`/`worker-reaped` event names and fields are
a stable log schema; tests against the fake worker
(`tests/conftest.py::fake_worker`, `tests/test_workers.py` style) — a
regression test should simulate the dying-listener window (old socket still
answering healthz while the new process hasn't bound).
