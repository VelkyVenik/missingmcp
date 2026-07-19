# 01 — PostHog platform research

Type: research
Status: claimed

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
