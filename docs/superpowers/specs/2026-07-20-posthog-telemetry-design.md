# PostHog Telemetry Design

Date: 2026-07-20. Decisions made ticket-by-ticket on the wayfinder map
`.scratch/posthog-telemetry/` (research asset:
`.scratch/posthog-telemetry/assets/posthog-platform-research.md`, facts verified
2026-07-19). This is **telemetry INTO PostHog for the operator** — not a user-facing
connector adapter, despite living on the `feature/posthog-adapter` branch.

## Goal

missingmcp.com usage statistics and logs flow to PostHog (EU cloud) so the operator can
(a) build campaign analytics — UTM-tagged visit → connect → active usage — and (b) query
usage and logs conversationally via PostHog's own MCP server connected to Claude.
Existing Slack reporting (`report.py`, `scripts/hourly_digest.py`) and the SQLite metrics
stay untouched as backup. Forward-only: no historical backfill.

## Research summary (2026-07)

- EU ingestion: `https://eu.i.posthog.com` — `/batch/` (<20MB body, no rate limits),
  public `phc_` project key in the JSON body. PostHog Logs (beta, launched 2025-12) is a
  generic OTLP receiver at `/i/v1/logs` (`Authorization: Bearer <phc_>`), 14-day
  retention. Railway has no log drains — log shipping must originate in the app.
- `posthog` Python SDK v7.27: thread-based background consumer (queue 10 000,
  flush_at=100, flush_interval=5 s), non-blocking `capture()`, `shutdown()` flushes;
  deps: `requests`, `backoff`, `distro`, `typing-extensions`. Python ≥3.10.
- Pricing: 1M events/mo free (no card); our volume ≈ 20–30K/mo ⇒ $0 with ~30×
  headroom. Identified events ≈5× anonymous price — immaterial at our volume. Free tier
  caps insight alerts at 5/org, hourly cadence minimum.
- posthog-js: default pageview/pageleave/autocapture; all UTM params auto-captured as
  event + initial/latest person properties; `cookieless_mode: 'on_reject'` is the
  consent-light EU posture.
- PostHog ships canonical MCP analytics event definitions (`$mcp_tool_call`,
  `$mcp_initialize`, `$mcp_tools_list`, `$mcp_custom`) with a built-in query family
  (`query-mcp-tool-stats`, `query-mcp-harness-breakdown`, …).
- Project exists: **MissingMCP.com**, id 227772, org VaclavSlajs, `eu.posthog.com`
  (Frankfurt). PostHog MCP already connected to the operator's Claude via the
  first-party claude.ai connector (OAuth — no personal API key needed).

## Decisions

1. **Identity**: `distinct_id` = the plain normalized login email (`account_key` without
   adapter scoping) — person = human, `adapter` is an event property. PostHog is the
   same trust class as Railway logs (which already carry `account=<email>`); person
   deletion joins the revoke runbook.
2. **Stitching**: server-side — posthog-js runs on landing *and* OAuth form pages (same
   domain ⇒ same anonymous cookie); on successful authorize the gateway sends
   `$identify` with `distinct_id`=email and `$anon_distinct_id` from the `ph_*_posthog`
   cookie. Accepted loss: cross-device journeys don't stitch.
3. **Egress rule — "identity and metadata yes, content never"**: allowed — email as
   distinct_id only, adapter, tool name, status, latency/bytes, UTM, page paths,
   user-agent; never — MCP request/response bodies (health data!), passwords / tokens /
   codes, form contents (incl. suggestion text — counted, not shipped). Raw IP discarded
   project-wide (GeoIP country/city kept). Autocapture only on marketing pages.
4. **Ingestion**: in-process via the official `posthog` Python SDK (operator's call:
   less owned code, free exception autocapture; the 4 deps and threads-beside-asyncio
   are accepted). One client per process, created at lifespan startup, `shutdown()` via
   `asyncio.to_thread` at exit.
5. **Logs**: OTLP export directly from the app to PostHog Logs, tee'd beside the
   existing stdout JSON stream — stdout→Railway stays the durable archive; the hourly
   digest keeps working unchanged. Beta status and 14-day retention accepted knowingly.
6. **Failure semantics & gating**: telemetry (events + logs) activates only when
   `POSTHOG_API_KEY` is set — a clean no-op otherwise (mirrors `BACKUP_S3_*` /
   `SLACK_WEBHOOK_URL` gating). A PostHog outage costs nothing: bounded queues, drop on
   overflow, never block a request, never crash, at most one warn per failure mode.
   Event loss during outages accepted (analytics, not accounting).
7. **Event taxonomy**: canonical `$mcp_*` events for MCP traffic; a lean snake_case
   funnel/conversion set for everything else; ops stays in the log channel. Event names
   and properties are a **stable schema** (same discipline as the log-event invariant).

## Architecture

### Configuration (`config.py`)

Two new optional fields, read from env:

| Env var | Meaning | Default |
|---|---|---|
| `POSTHOG_API_KEY` | public `phc_` project key; **gates all telemetry** | unset → telemetry off |
| `POSTHOG_HOST` | ingestion host | `https://eu.i.posthog.com` |

The key lives in Railway env vars (it is public-class — it ships in web pages — but is
still configuration, never committed). No other secrets: log/OTLP auth reuses the same
`phc_` key.

### `telemetry.py` (new module)

Owns the SDK client and the OTLP log tee behind one seam:

- `Telemetry(config)` — no-op object when `POSTHOG_API_KEY` is unset (every method a
  cheap early return; mirrors `Backup.enabled`).
- `capture(event, distinct_id, properties, anonymous=False)` — thin wrapper over the SDK
  client's non-blocking enqueue. `anonymous=True` sets `$process_person_profile: false`
  (keeps subscribe/suggest on anonymous pricing and out of person profiles).
- `identify(email, anon_distinct_id)` — the stitch event.
- `anon_id_from_cookie(request)` — parses the `ph_<key>_posthog` cookie, returns the
  anonymous distinct_id or `None`. Pure function, unit-testable.
- Lifespan wiring in `app.py`: construct at startup, `await asyncio.to_thread(shutdown)`
  on exit.
- SDK's own stdlib logging flows through the existing `_StructuredHandler` bridge — no
  plain-text stderr (verify at implementation).

### Server events

| Event | Emitted from | distinct_id | Properties |
|---|---|---|---|
| `$mcp_tool_call` | `proxy.handle_mcp`, beside the `mcp-response` log | account email | tool, adapter, status, ttfb_ms, total_ms, bytes |
| `$mcp_initialize` | proxy, on JSON-RPC `initialize` | account email | client name/version from `params.clientInfo`, adapter |
| `$mcp_tools_list` | proxy, on `tools/list` | account email | adapter |
| `login_succeeded` / `login_failed` | `oauth.authorize_post` / callback | account email (failed: form email) | adapter, reason (failed only; error class, never credentials) |
| `mfa_challenged` | oauth, on `SecondFactorNeeded` | account email | adapter |
| `account_connected` | `oauth._finish` (after verify) | account email | adapter, `status: new\|returning` (from the upsert) |
| `$identify` | `oauth._finish`, when the request carries a posthog cookie | account email | `$anon_distinct_id` = cookie anon id |
| `account_revoked` | `scripts/revoke.py` (best-effort) | account email | adapter |
| `subscribe` / `suggest` | `app.py` POST handlers | cookie anon id (anonymous) | none (email/suggestion text stay local per the egress rule) |

Exact `$mcp_*` property keys must match what `@posthog/mcp` emits (so the built-in
`query-mcp-*` tools light up) — **verify the exact key names against the `@posthog/mcp`
source at implementation time**; the fresh project's taxonomy doesn't materialize them
until first ingest.

Worker lifecycle, WHOOP refreshes, backups, cleanups: **not events** — they are logs and
arrive in PostHog via the OTLP tee.

### Log tee (OTLP)

- New deps: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`.
- `LoggerProvider` + `BatchLogRecordProcessor` → `OTLPLogExporter` at
  `{POSTHOG_HOST}/i/v1/logs`, `Authorization: Bearer <phc_>`.
- A `logging.Handler` attached in `log.py` beside the stdout stream when telemetry is
  enabled — every structured event (and bridged stdlib/uvicorn records) tees to PostHog;
  the stdout format is unchanged byte-for-byte.
- **Loop guard**: the OTel exporter's own loggers are excluded from the tee (an export
  failure must not generate a log that re-enters the exporter).
- Drop-on-overflow batching; Railway remains the archive (PostHog retention: 14 days).

### Web (posthog-js)

- Snippet rendered by `pages.py` into `templates/_layout.html` only when telemetry is
  enabled (key + host threaded through `render_page`).
- Marketing pages (home, connector landings): full defaults — pageview/pageleave,
  autocapture, auto-UTM, `person_profiles: 'identified_only'`,
  `cookieless_mode: 'on_reject'` with a lightweight footer consent affordance (no
  blocking banner) — the exact consent UX is an implementation-time detail to verify
  against posthog-js's consent API.
- OAuth/sign-in pages: snippet with `autocapture: false`, explicit `$pageview` only
  (pages with credential forms get belt-and-suspenders treatment).

### Campaign model

- Canonical funnel: `$pageview` (UTM-tagged) → authorize-form `$pageview` →
  `account_connected` (`status: new`) → first `$mcp_tool_call`.
- UTM discipline — every link the operator shares carries `utm_source` +
  `utm_campaign`; `utm_medium` optional. Agreed `utm_source` values: `linkedin`,
  `twitter`, `reddit`, `hn`, `blog`, `dm`, `other` (extend the table here when a new
  channel appears; slugs for `utm_campaign` are free-form-but-stable, e.g.
  `garmin-launch-2026-07`).

### Files touched

- `src/missingmcp/telemetry.py` — new (client, capture/identify, cookie parse, OTLP tee).
- `config.py` — `posthog_api_key`, `posthog_host`.
- `log.py` — attach the tee handler when enabled.
- `app.py` — lifespan init/shutdown; `subscribe`/`suggest` capture.
- `proxy.py` — `$mcp_*` capture beside `mcp-response`.
- `oauth.py` — funnel events + `$identify`.
- `pages.py`, `templates/_layout.html` (+ OAuth form templates) — snippet injection.
- `scripts/revoke.py` — best-effort `account_revoked`; README revoke runbook gains the
  "delete the person in PostHog" step.
- `pyproject.toml` — `posthog`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`.
- `README.md` — env-var reference + Monitoring section (PostHog alongside Slack).

## Privacy & security invariants

- The egress rule (Decision 3) is enforced at the `telemetry.py` seam: call sites pass
  explicit property dicts; no kwargs-passthrough of request data.
- Existing invariants carry: secrets never logged ⇒ never captured; the OTLP tee ships
  exactly what stdout ships (which already honors the secrets invariant).
- Raw IP: "Discard client IP data" enabled in project settings (rollout step) — applies
  to both SDK and posthog-js events.
- GDPR posture: EU cloud (Frankfurt), PostHog DPA, person deletion on revoke (runbook,
  UI — ~10 users, rare enough that no automation is warranted).

## Rollout & verification

1. Project settings: enable "Discard client IP data"; confirm person profiles =
   identified-only default.
2. Set `POSTHOG_API_KEY` (+ optionally `POSTHOG_HOST`) in Railway; push to main
   (= deploy).
3. Smoke: visit the landing via a UTM-tagged link → connect a test account (Garmin) →
   run one tool call → verify in PostHog: `$pageview` with UTM, `$identify` stitch (one
   person, email distinct_id, UTM person props), `account_connected`, `$mcp_tool_call`
   with correct properties, log rows arriving in Logs, and `query-mcp-tool-stats`
   answering via the Claude-connected PostHog MCP.
4. Confirm the no-op path: unset key locally → gateway starts clean, zero PostHog
   traffic (existing tests keep passing with telemetry off).
5. Dashboards/insights/alerts: built in PostHog after data flows (deliberately not
   specced — see map fog).

## Testing

- `tests/test_telemetry.py`: no-op contract when disabled; capture/identify enqueue to a
  stubbed SDK client; `anon_id_from_cookie` against real cookie fixtures; loop-guard
  logger exclusion.
- OAuth flow tests assert funnel events + `$identify` via an injected telemetry stub
  (mirrors how `spawn` is injectable in `workers.py`); proxy tests assert `$mcp_tool_call`
  beside the existing `mcp-response` assertions.
- The OTLP tee: handler-attach unit test; export itself is fire-and-forget
  vendor code — not re-tested.
- Release gate (matches repo culture): the manual smoke in Rollout step 3 against the
  real EU project.

## Out of scope

- Agents over the data in PostHog (future effort, once data flows).
- Retiring Slack reports / SQLite metrics — kept as backup by operator decision.
- Historical backfill; session replay, experiments, feature flags, surveys.
- Dashboard/alert build-out (post-data work; free tier caps alerts at 5/org — the
  hourly digest is complemented, not replaced).
