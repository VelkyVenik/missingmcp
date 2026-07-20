# 04 — Ingestion architecture

Type: grilling
Status: resolved
Blocked by: 01

## Question

How do events physically get from the gateway to PostHog?

1. **In-process capture** (emit at source: proxy/oauth/app call a small PostHog client —
   SDK or dependency-free httpx per the `backup.py` precedent; batching/flush in the
   lifespan loop; must never block the event loop or fail a request) vs. an **external
   shipper** (Railway logs → PostHog, hourly_digest-style standalone job) vs. a mix.
2. **What happens to full LOGS**: ship them (PostHog Logs / error tracking, if viable per
   ticket 01) or keep logs in Railway and send only curated events?
3. **Failure semantics**: PostHog being down must cost nothing — fire-and-forget, drop on
   overflow (mirrors `backup.run` never raising).

## Answer

Decided with the operator, 2026-07-20:

1. **In-process capture via the official `posthog` Python SDK** — the operator's call
   over the hand-rolled httpx client after a side-by-side comparison: fewer lines to
   own, battle-tested batching, and exception autocapture / feature flags come free.
   Accepted costs, recorded knowingly: 4 new deps (incl. a second HTTP stack,
   `requests`) and the SDK's thread-based consumer beside the asyncio loop. One client
   per process, created at lifespan startup (`host=https://eu.i.posthog.com`),
   `shutdown()` via `to_thread` at lifespan exit; `capture()` is a non-blocking enqueue
   (queue 10000, flush_at=100 / 5 s). The external log-shipper alternative died with
   ticket 03's stitching decision (it never sees the request cookie).
2. **Logs: OTLP export directly from the app** to PostHog Logs
   (`eu.i.posthog.com/i/v1/logs`, Bearer `phc_`) via an OTel logging handler attached
   beside the existing stdout JSON stream. stdout→Railway stays the durable archive
   (PostHog Logs is beta, 14-day retention — accepted; logs-in-PostHog was a core goal
   of the effort) and the hourly digest keeps working unchanged.
3. **Fire-and-forget + env-gate**: telemetry (events and OTLP logs) activates only when
   the `POSTHOG_*` env vars are set — a clean no-op otherwise, mirroring the
   `BACKUP_S3_*`/`SLACK_WEBHOOK_URL` gating. A PostHog outage must cost nothing:
   bounded queues, drop on overflow, never block a request or crash the process, at
   most one warn per failure. Event loss during outages is accepted (analytics, not
   accounting).
