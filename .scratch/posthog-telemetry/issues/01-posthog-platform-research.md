# 01 — PostHog platform research

Type: research
Status: resolved

## Question

Surface the PostHog platform facts the design decisions (tickets 03–05) wait on. EU cloud
(eu.posthog.com) is decided; the consumer is a single-node Python 3.12 Starlette+httpx
gateway on Railway plus server-rendered landing pages. Needed facts:

1. **Server-side ingestion**: the capture / batch API shape on EU cloud (endpoints, auth,
   payload, size/rate caps); the `posthog` Python SDK — footprint, background
   queueing/flush behavior, asyncio-friendliness — vs. calling the HTTP API directly from
   httpx (this repo's dependency-light precedent is `backup.py`'s SigV4 signer).
2. **Logs**: what exists for shipping/querying LOGS in PostHog (Logs product status?,
   error tracking) vs. modeling everything as events; any Railway → PostHog log-drain path.
3. **Pricing/limits**: free tier and per-event price; the anonymous vs. identified events
   pricing distinction; person-profile costs — what a small gateway (a handful of users
   plus landing-page traffic) would actually consume.
4. **Web analytics**: posthog-js — autocapture vs. explicit events, pageview/pageleave,
   UTM campaign attribution, cookieless/consent options relevant for a small EU site.
5. **Identity**: distinct_id semantics, `identify`/`alias` stitching of anonymous web
   visitors to known accounts; person properties; GDPR tooling (deletion, data residency).
6. **Alerts/integrations**: insight alerts → Slack (could complement the existing digest).
7. **PostHog MCP**: what the official MCP server can query (insights, HogQL, usage data),
   auth (personal API key), EU-region support — feeds ticket 06.

Record the findings as `assets/posthog-platform-research.md` (oura precedent) and resolve
this ticket with the gist.

## Answer

Full findings: [assets/posthog-platform-research.md](../assets/posthog-platform-research.md) (verified 2026-07-19 against posthog.com/docs, pricing, PostHog GitHub, docs.railway.com).

- **Ingestion**: EU capture is `POST https://eu.i.posthog.com/i/v0/e/` (single) and
  `/batch/` (<20MB body, unlimited events, **no rate limits**); auth = public `phc_` key
  in the JSON body — httpx-direct is trivial (simpler than backup.py's SigV4). The
  `posthog` SDK (v7.27.0) is thread-based (queue 10000, flush_at=100, 5s interval, no
  asyncio), lifespan-friendly, but drags in `requests`+3 deps → httpx-direct wins.
- **Logs**: PostHog Logs is **beta** (launched 2025-12-23), a generic OTLP receiver at
  `https://eu.i.posthog.com/i/v1/logs` (Bearer `phc_` token), 14-day retention, free at
  our volume. **Railway has no log drains** — app-side OTel export or a Vector sidecar.
  Events-first, Logs optional later.
- **Pricing**: 1M events/mo free; anonymous $0.00005/event after, identified
  ~$0.000248/event (person processing is the surcharge, no separate profile fee).
  ~10 users ≈ 20–30K events/mo ⇒ **$0**. Free tier caps alerts at 5/org.
- **Web**: posthog-js autocaptures + auto-UTM (initial/latest person props);
  `person_profiles: 'identified_only'` default; `cookieless_mode: 'on_reject'` gives a
  consent-light EU posture. **Identity**: use `SHA-256(adapter:email)` as distinct_id
  (account_key is PII); `identify()` on the connect-success page stitches UTM visitor →
  account; EU data in Frankfurt; person+events deletion via UI/API (async).
- **Alerts**: insight alerts post to Slack natively (trends/funnels/HogQL; hourly+ on free).
- **MCP**: hosted `https://mcp.posthog.com/mcp`, OAuth or `phx_` key (MCP-Server preset),
  **EU auto-routed**, tools incl. insights + HogQL; claude.ai has a first-party PostHog
  connector — ticket 06 is likely just OAuth, mooting ticket 02's parked API-key question.
