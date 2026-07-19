# 04 — Ingestion architecture

Type: grilling
Status: open
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
