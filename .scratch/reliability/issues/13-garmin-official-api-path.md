# 13 — Official Garmin API path: watch the paused developer program, keep an application ready

Type: strategy (watch)
Status: needs-info (blocked on Garmin reopening the program)

## Why

Today's Garmin access (garminconnect + user credentials) is unofficial and
fragile: Garmin broke the ecosystem's auth flow in March 2026 (garth is
deprecated since), and our shared egress IP is Cloudflare-rate-limited on
the SSO portal (ticket 12). The legitimate alternative exists — the
**Garmin Connect Developer Program**: Health / Activity / Training /
Courses / Women's Health APIs, OAuth 2.0 + PKCE user consent (the WHOOP
model), push webhooks + limited pull, no license fees.

## Why not now (researched 2026-09-21)

- **The program is paused for new applicants since spring 2026** — the
  application form was removed ("Stay tuned for more updates"), Partner
  Services cites a "significant redesign and modernisation of the API
  program", no timeline, no waitlist. Context: July 2025 API Brand
  Guidelines → Strava lawsuit (withdrawn after 3 weeks) → freeze.
  Existing partners are unaffected.
- **Eligibility (pre-pause): legal entities only** — company, university,
  hospital, research. Personal-use applications were rejected. Applying
  will need an IČO/s.r.o., not a personal account.
- **Coverage differs from garmin_mcp**: sleep/HRV/dailies/stress/Body
  Battery/VO2max/activities/workout-write are covered; gear management
  (our 2nd most-used tool), nutrition (search_foods), badges, race
  predictions are not. Migration would be a new, smaller adapter (like
  whoop), not a 1:1 swap — possibly run *alongside* the current one.

## How to watch

- Periodically check https://developer.garmin.com/gc-developer-program/
  for the application form returning (monthly is enough; no waitlist
  exists, so the page is the only signal).
- When it reopens: apply early (expect a rush). Application draft below.

## Application draft (fill on reopening)

- Entity: TBD (Václav — vlastní IČO/s.r.o.; NOT YSoft).
- Use case: multi-user MCP gateway letting Garmin users query their own
  data conversationally in Claude/ChatGPT (missingmcp.com, ~1 800
  connected accounts, ~650 weekly active). OAuth consent per user, data
  never stored beyond encrypted tokens, aggregate-only telemetry.
- APIs: Health (dailies, sleep, HRV, stress, user metrics), Activity,
  Training (workout push), Courses.
- Webhook endpoint: the gateway can host `/garmin/webhook` (new build).

## Related

- Ticket 12 (SSO egress rate-limiting) — the pain this path would
  eliminate; its probe (`sso-probe`) and breaker are interim mitigations.

## Sources (2026-09-21)

- the5krunner.com/2026/09/14/garmin-developer-api-access-paused/
- developer.garmin.com/gc-developer-program/ · developerportal.garmin.com
- ghurt.org/garmin-api-for-personal-use (legal-entity requirement; garth
  deprecation, March 2026)
