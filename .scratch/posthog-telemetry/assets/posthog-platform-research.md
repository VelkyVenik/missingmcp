# PostHog platform research (ticket 01)

Researched 2026-07-19 against live primary sources (posthog.com docs/pricing, PostHog GitHub,
docs.railway.com). Anything not confirmed from a primary source is marked **UNVERIFIED**.
Consumer context: single-node Python 3.12 Starlette+httpx gateway on Railway, EU cloud
(project already created — ticket 02), a handful of trusted users, server-rendered landing
pages, dependency-light ethos (`backup.py` SigV4 precedent).

---

## 1. Server-side ingestion (capture/batch API, Python SDK vs httpx)

### HTTP capture API — EU cloud

- **Single event:** `POST https://eu.i.posthog.com/i/v0/e/`
- **Batch:** `POST https://eu.i.posthog.com/batch/` — "There is no limit on the number of
  events you can send in a batch, but the entire request body must be less than 20MB by
  default."
- **Auth:** the **public project API key (`phc_…`) goes in the JSON body** as `api_key` —
  no auth header, no secret key. (The `phc_` token is the same one embedded in web pages;
  it can only write events, not read data.)
- **Payload (single event):**

  ```json
  {
    "api_key": "phc_...",
    "event": "mcp_tool_call",
    "distinct_id": "garmin:3f9a2c...",
    "properties": {"adapter": "garmin", "tool": "get_sleep_data", "status": 200},
    "timestamp": "2026-07-19T12:34:56Z"
  }
  ```

  **Batch:** `{"api_key": "...", "historical_migration": false, "batch": [ {"event": ...,
  "distinct_id": ..., "properties": {...}, "timestamp": ...}, ... ]}`.
- **Rate limits: none on capture.** "For public POST-only endpoints like event capture
  (`/i/v0/e`) and feature flag evaluation (`/flags`), there are no rate limits." The query
  (read) API is what's rate-limited, not ingestion.
- Per-single-event size cap: not documented on the capture page (infra-level Kafka caps
  exist per GitHub issues) — **UNVERIFIED**; irrelevant at this repo's payload sizes
  (small property dicts).
- Sources: https://posthog.com/docs/api/capture,
  https://github.com/PostHog/posthog.com/blob/master/contents/docs/api/capture.mdx,
  https://posthog.com/docs/api (rate-limit statement),
  https://posthog.com/docs/advanced/proxy/troubleshooting (region hosts).

### `posthog` Python SDK

- Package `posthog`, current **v7.27.0**, requires **Python >= 3.10** (3.12 OK). Runtime
  deps: `requests>=2.7,<3.0`, `backoff>=1.10.0`, `distro>=1.5.0`,
  `typing-extensions>=4.2.0` — i.e. it drags in a **second HTTP stack (`requests`)**
  alongside this repo's httpx.
  Source: https://github.com/PostHog/posthog-python/blob/master/pyproject.toml
- **Pure threads, no asyncio.** `capture()` is non-blocking: it does
  `queue.Queue(max_queue_size).put(msg, block=False)`; daemon `Consumer` threads batch and
  POST via `requests`. Defaults: `max_queue_size=10000`, `flush_at=100`,
  `flush_interval=5.0` s, `thread=1` consumer, `sync_mode=False`. `shutdown()` =
  `flush()` (queue drain, 10 s default timeout) + `join()`; also `atexit`-registered.
  Source: https://github.com/PostHog/posthog-python/blob/master/posthog/client.py
- Starlette compatibility: fine in practice — instantiate one client at lifespan startup,
  call `posthog.shutdown()` at lifespan exit; daemon threads coexist with the event loop
  (same pattern the repo's `_bounded`/`to_thread` already accepts). v7 pushes a
  context-based API (`new_context()` / `identify_context()`); backend events default to
  **identified** unless `$process_person_profile: false` is set.
  Source: https://posthog.com/docs/libraries/python,
  https://posthog.com/docs/data/anonymous-vs-identified-events
- EU host: `Posthog('<phc_token>', host='https://eu.i.posthog.com')`.

### Implications for this repo

Direct **httpx to `/batch/`** is genuinely trivial here — JSON body with the `phc_` key,
no signing (far simpler than `backup.py`'s SigV4), no rate limits, 20MB ceiling never
approached. The only things the SDK would buy are the background queue/flush/retry
(`backoff`) — a small amount of code the app lifespan loop (backup/report precedent)
can replicate with an in-memory list + periodic `httpx` batch flush. The SDK is
acceptable (small, thread-based, lifespan-friendly) but adds `requests` + 3 more deps;
the dependency-light call goes to httpx-direct. Fire-and-forget + bounded queue keeps
the MCP hot path unaffected.

---

## 2. Logs (product status, error tracking, Railway drain path)

- **PostHog Logs exists and is in BETA** — announced 2025-12-23 ("Meet Logs (beta)",
  https://posthog.com/blog/logs-beta). It is a **generic OTLP receiver**: "PostHog Logs
  works with any OpenTelemetry-compatible client. You don't need any PostHog-specific
  packages."
- **EU OTLP endpoint:** `https://eu.i.posthog.com/i/v1/logs` (US:
  `https://us.i.posthog.com/i/v1/logs`), auth
  `Authorization: Bearer <phc_project_token>` (or `?token=` query param). Explicitly:
  use the `phc_` project token, **not** a personal `phx_` key.
  Sources: https://posthog.com/docs/logs/installation/python,
  https://posthog.com/docs/logs/installation
- Python ships logs via the standard OTel SDK (`LoggerProvider` +
  `BatchLogRecordProcessor` + `OTLPLogExporter` + a stdlib `LoggingHandler`); note the
  OTel Python logs API is still experimental (`opentelemetry._logs` private imports) —
  that's several new dependencies (opentelemetry-sdk + exporter).
- **Retention 14 days** default (30/90-day options "coming soon"). Pricing: docs
  disagree with themselves — /docs/logs says "first 50GB free, then $0.25/GB up to
  300GB, then $0.15/GB"; /docs/logs/start-here says "first 10 GB free, $0.25/GB with
  discounts". Which is current is **UNVERIFIED**, but immaterial: this gateway's
  structured stdout is well under 1 GB/month either way — free.
- Querying: search UI with token/negative/exact-phrase filters + AI search; deeper
  error-tracking/replay correlation on the roadmap.
- **Railway has NO log drains**: "Railway does not have a setting to forward stdout from
  a service to an external intake URL." Recommended alternatives are app-side SDK/OTel
  export, or a separate log-forwarder service (Vector / Fluent Bit / OTel Collector) on
  Railway. Source: https://docs.railway.com/guides/third-party-observability
- **Error Tracking** is a separate, live PostHog product (exception capture, grouping
  into issues, alerts): 100K exceptions/month free, then $0.000370/exception
  (https://posthog.com/pricing, https://posthog.com/docs/error-tracking). The Python SDK
  lists error tracking among its features.

### Implications for this repo

Events-only is NOT the forced model — Logs is real, OTLP-shaped, and free at our volume —
but it's **beta, 14-day retention, and needs either OTel deps in-process or a Vector
sidecar service on Railway** (no drain). The pragmatic split: model *analytics-worthy*
things as capture events (cheap, durable, insight-able), treat Logs as an optional
add-on for raw `log.py` output later (a `_StructuredHandler`-style OTLP tee is easy to
bolt on), and keep Railway logs + hourly digest as the operational backstop (already
decided as backup).

---

## 3. Pricing / limits

- **Free tier: 1M product-analytics events/month, no credit card**; also 100K
  exceptions, 5K session recordings, 1M feature-flag requests, 1500 survey responses.
  Source: https://posthog.com/pricing
- **Anonymous events** (past free tier): $0.00005/event at 1–2M, stepping down to
  $0.000009 at 250M+.
- **Identified events cost more**: person-profile processing is billed as an add-on on
  top of the base event price — billing API shows the "enhanced persons" component at
  **$0.000198/event** (1–2M tier) on top of $0.00005 base ⇒ effective
  **~$0.000248/identified event** at the first paid tier (matches third-party teardowns).
  Official phrasing: "anonymous events can be up to 4x cheaper than identified ones."
  Person profiles have **no separate monthly fee** — you pay via the identified-event
  rate; profiles are only created for identified events.
  Sources: https://posthog.com/docs/data/anonymous-vs-identified-events,
  https://billing.posthog.com/api/products-v2 (tier data),
  https://posthog.com/pricing
- Alerts: **free-tier orgs capped at 5 alerts** (see §6).

### Cost estimate for this gateway

~10 users × generous 50 tool calls/day ≈ 15K identified events/month; OAuth/connect
lifecycle events: hundreds; landing pages (pageview + pageleave + autocapture on small
campaign traffic): a few thousand anonymous events. Total ≈ **20–30K events/month ≈ 2–3%
of the free tier ⇒ $0/month**, with ~30x headroom. Even fully identified and 10x over
today's traffic it stays free. Logs volume ≪ free tier. Realistic bill: **$0**.

### Implications for this repo

Cost is a non-issue at this scale; the anonymous/identified distinction matters for
*model correctness*, not money — so choose identified events wherever user-level
funnels are wanted, and don't contort the design to save fractions of a cent.

---

## 4. Web analytics (posthog-js on the landing pages)

- Setup: HTML snippet (recommended) or npm; `api_host: 'https://eu.i.posthog.com'`.
  Source: https://posthog.com/docs/libraries/js
- **Defaults** (https://posthog.com/docs/libraries/js/config):
  `autocapture: true` (clicks/inputs/etc.), `capture_pageview: true` (with
  `defaults: '2025-05-24'` or later, pageviews use `history_change` — SPA-safe; moot for
  server-rendered pages), `capture_pageleave: true`, `capture_dead_clicks: true`.
- **UTM attribution is automatic**: all five `utm_source|medium|campaign|content|term`
  are captured as event properties, as person properties in **initial + latest** variants
  (`$initial_utm_source`, …), and as session "entry" properties; extra params via
  `custom_campaign_params`. Insights/web analytics can filter and break down by them
  directly. Source: https://posthog.com/docs/data/utm-segmentation
- **`person_profiles: 'identified_only'` is the default** — anonymous visitors don't
  create person profiles (cheap anonymous events) until `identify()` is called.
  Source: https://posthog.com/docs/data/anonymous-vs-identified-events
- **Cookieless / consent-light options** (relevant for a small EU site):
  - `cookieless_mode: 'on_reject'` — no cookies and no events until consent is
    given/denied; on denial PostHog still counts users via a **server-generated
    privacy-preserving hash** (no distinct_id stored in the browser).
  - `persistence: 'memory'` — nothing persisted beyond the pageview; switchable at
    runtime via `posthog.set_config()` after consent.
  - `opt_in_capturing()` / `opt_out_capturing()` (+ `opt_out_capturing_by_default`).
  - PostHog's GDPR guidance still says: if you use cookies for logged-out users, show a
    banner; cookieless mode is the way to avoid one.
  Sources: https://posthog.com/tutorials/cookieless-tracking,
  https://posthog.com/docs/libraries/js/persistence,
  https://posthog.com/docs/privacy/gdpr-compliance
- PostHog's Web Analytics dashboard (GA-style) is fed by these same `$pageview` events —
  no separate product fee at this scale.

### Implications for this repo

The snippet drops into `templates/_layout.html` once (whole site covered). Autocapture +
default pageview/pageleave gives the traffic picture for free; the campaign funnel
(UTM visit → connect) additionally wants **explicit events at the connect-flow steps**
(authorize form shown / login OK) since those pages are also served by the gateway.
`identified_only` + `cookieless_mode: 'on_reject'` (or memory persistence) is the
consent-light EU posture appropriate for a hobby-scale site.

---

## 5. Identity (distinct_id, stitching, GDPR)

- **distinct_id** = the per-user identifier on every event; "usually something stable
  like a UID, their email, or their database ID"; best practice: stable unique strings.
  posthog-js mints an anonymous device ID automatically; backend events must supply one.
  Source: https://posthog.com/docs/product-analytics/identify
- **`identify()`** (frontend) "merges the anonymous person *into* the identified person,
  linking the two IDs" — pre-login anonymous events join the identified profile. Person
  properties via `$set` / `$set_once`. **`alias()`** attaches an additional distinct_id
  to an existing person (advanced; use when identify's merge direction doesn't fit).
  Merging two *already-identified* users is restricted.
- **GDPR tooling:** PostHog Cloud EU is "hosted on servers based in Frankfurt". Deletion:
  persons UI or API deletes a person **and all their events**; deletions are processed
  **asynchronously**; do not reuse a deleted `distinct_id` while deletion is in flight
  (a "Reset deleted person" tool exists for afterwards).
  Sources: https://posthog.com/docs/privacy/gdpr-compliance,
  https://posthog.com/docs/privacy/data-deletion

### Implications for this repo

`account_key` is a lowercased email — **PII**, and the map forbids leaking it. Use a
pseudonymous distinct_id: **`SHA-256(adapter + ":" + account_key)`** (full hash, not the
8-char log prefix — that convention is for log readability, not identity) or
`adapter:sha256(email)`; keep adapter as an event property either way. Deletion per user
then means: compute the hash, hit the persons deletion API — clean right-to-be-forgotten
story with zero emails in PostHog. Stitching for campaign analytics: the connect flow's
authorize/login pages are served by this gateway, so posthog-js is present — call
`posthog.identify(<hashed id>)` on the post-connect success page (or pass the JS
anonymous ID through the form and `alias` server-side) to weld UTM-attributed visitor →
account → subsequent server-side tool-call events into one person.

---

## 6. Alerts / integrations (→ Slack)

- **Insight alerts support Slack natively**: destinations are "email recipients, Slack
  channels, Discord webhooks, Microsoft Teams channels, or webhook URLs directly in the
  alert form." Source: https://posthog.com/docs/alerts
- Alerts work on **trends, funnels (steps + trends views), and SQL (HogQL) insights**;
  conditions: absolute threshold ("has value") or relative increase/decrease (value or
  %). Check frequency: hourly/daily/weekly/monthly free; every-15-min needs the Boost
  plan; real-time is Scale/Enterprise. **Free tier: max 5 alerts per org.**
- Separately, insight/dashboard **subscriptions** can post scheduled snapshots to Slack.

### Implications for this repo

A "below threshold" (has-value) alert on an hourly/daily trend can detect *silence*
(events stopped) — so PostHog alerts could eventually cover the digest's
anomaly/heartbeat role, as the map already flags for later. The 5-alert free cap and
hourly-minimum cadence mean the GitHub-Actions hourly digest stays the sharper
operational tool for now; PostHog alerts complement (e.g. error-spike, zero-connects).

---

## 7. PostHog MCP server

- **Hosted endpoint: `https://mcp.posthog.com/mcp`** (streamable HTTP); a legacy SSE
  endpoint exists at `https://mcp.posthog.com/sse`. Free to use ("connecting to the MCP
  server and calling its tools is free"), though "some tools use LLMs internally" and
  require org-level AI processing enabled (billed as PostHog AI spend).
- **Auth:** OAuth "out of the box with the wizard" for supported clients, **or** a
  personal API key (`phx_…`) created with the **"MCP Server" preset**
  (`https://app.posthog.com/settings/user-api-keys?preset=mcp_server`), sent as
  `Authorization: Bearer <key>`.
- **EU support: automatic.** "The MCP server acts as a proxy to your PostHog instance
  and is automatically routed to the correct region (US or EU) based on the account you
  sign in with." No EU-specific URL needed.
- **Tools/capabilities:** product analytics (insights, dashboards), **SQL/HogQL
  queries**, feature flags, error tracking, experiments, surveys, CDP; config supports
  read-only mode, tool filtering by feature/name, and pinning to a specific org/project
  via headers or query params. Rate limits = the normal PostHog API limits (no separate
  MCP layer).
- **Claude setup:** `npx @posthog/wizard mcp add` auto-configures Claude Code / Claude
  Desktop (also Cursor, VS Code, Zed…); manual Claude Desktop config uses `mcp-remote`
  with the Bearer header. Additionally, **claude.ai already ships a first-party PostHog
  connector** (OAuth-based; observed live in this workspace's connector list) — for the
  operator's claude.ai/mobile use, ticket 06 may be a "click Connect" task, no key
  handling at all.
- Sources: https://posthog.com/docs/model-context-protocol,
  https://posthog.com/docs/model-context-protocol/faq,
  https://github.com/PostHog/mcp

### Implications for this repo

Ticket 06 is low-risk: hosted server, EU auto-routed, OAuth preferred (which likely moots
the parked personal-API-key question from ticket 02); HogQL access means ad-hoc questions
over the event taxonomy work from day one — one more reason to get event/property names
right in the taxonomy ticket.
