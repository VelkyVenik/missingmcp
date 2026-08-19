# Post-fix verification — 7-day window 2026-08-12 → 2026-08-19

Measured 2026-08-19 ~19:50 UTC over **2026-08-12T00:00Z → run time** (~7.8
days; "per day" figures use 7.8). Source: PostHog Logs (service `missingmcp`),
`logs-count` scalars + a 740-row dump of non-teardown error/fatal rows parsed
locally with the digest's own folding rule (worker-log error rows whose line
lacks ERROR|CRITICAL are traceback continuations, not problems).

**Window caveats:** the originally-planned window (2026-07-31 → 08-07) was
lost — the scheduled cloud routine fired but never delivered its PR, and
PostHog Logs retention (~2 weeks) has since aged those rows out. This fresh
window measures steady state 12–19 days after the fixes, which is arguably
more representative. PR #18 (stream-teardown demotion) deployed 2026-08-19
~19:45Z — inside the window's last minutes — so teardown rows here are
pre-#18 severity; the "excluding teardown" figures are the forward-looking
ones.

## 1. Stale-token failures — the original problem

| metric | pre-fix baseline (7d) | this window | change |
|---|---|---|---|
| stale-token failure rows | 177 | **2** (`worker-forward-auth-stale`/`worker-exited-early` pairs) | **−99 %** |
| distinct accounts hit | 58 (a third of the base) | **2** | −97 % |
| real worker faults (`worker-start-failed`) | (split 07-26) | 2 (isolated rc=3 crashes, 2 accounts) | — |
| recovery | median ~35 h, 1 in 3 never | **both re-signed in within minutes: 2.4 min (jdb***), 7.8 min (max***)** | — |

The token read-back keeps working: **2,310 `worker-tokens-persisted`**
(~300/day) in the window. The fast recoveries also validate the PR #16
re-auth copy — both users followed the 401 instruction immediately.

## 2. Digest problem volume and loud hours

Total error/fatal rows: **3,841** (~490/day).

| class | rows | note |
|---|---|---|
| stream-teardown (gateway "Exception in ASGI application" + worker mirror line) | ~3,101 (81 %) | demoted to warn/info by PR #18 from 2026-08-19 on |
| `mcp-forward-error` | **463** | NEW growing class — see §3 |
| garminconnect portal sign-in failures (`Login failed: All login strategies exhausted`, mostly Cloudflare/HTTP blocks) | 134 | known upstream class |
| folded traceback continuations | 74 | not problems per digest folding |
| authorize-flow noise (`authorize-client-id-not-dcr` 23, `csrf-invalid` 11, `unknown-client` 4) | 38 | scanners/misconfig |
| `login-start-failed` (reason=auth) | 20 | users mistyping credentials |
| `mfa-resume-failed` | 4 | |
| worker port-bind + misc | 5 | |

Loud digest hours at ANOMALY_MIN=3 (baseline ~8.7/day pre-fix):

| scenario | loud hours /168 | per day | minor | silent |
|---|---|---|---|---|
| as measured (teardown still error) | ~all | ~24 | — | — |
| **post-#18 (teardown demoted)** | **81** | **~11.6** | 26 | 61 |
| post-#18 AND `mcp-forward-error` resolved | **17** | **~2.4** | 29 | 122 |

## 3. NEW class: `mcp-forward-error` (needs its own ticket)

463 rows, garmin only: **ConnectError 428 + ReadError 35** from
`proxy.handle_mcp`'s `client.send()` — the connection to the worker fails
before headers, the client gets a 502. Growing: 27→40→54→12→32→82→109→107
per day. **214 distinct accounts** (most of the base), dominated by handshake
methods: `server/discover` 286 (a non-standard method newer clients probe),
`initialize` 150. Mechanism not diagnosed here (out of this ticket's scope);
user impact is plausible (502 on initialize can surface as a connector error
in Claude) and it is the main remaining pager feeder post-#18.

## 4. Verdict for the mail-channel decision (ticket 07)

**No — an outbound mail channel is not justified.** Expiries collapsed from
58 accounts/7d to 2/7d (−97 %), and both affected users recovered within
minutes via the 401 + improved message (median ~5 min vs ~35 h baseline).
There is no population left for a proactive mail to help; the operational
cost (provider, secrets, deliverability, unsubscribe) buys nothing.
Revisit only if expiry volume regresses.
