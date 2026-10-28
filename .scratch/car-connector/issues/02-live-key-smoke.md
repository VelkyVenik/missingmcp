# 02 — Live key against own Škoda

Type: task
Status: resolved
Blocked by: 01

## Question

What do real Public API responses look like for Vaclav's own car? HITL: Vaclav generates an
`X-API-Key` in the MyŠkoda app; together we call the endpoints 01 surfaced (vehicle list /
status / SoC / climate state, one harmless action if budget allows) and record: response
shapes, observed `RateLimit-*` and `X-API-Key-Expires-At` values, error bodies. The key is
never committed or logged; responses are saved redacted (no key, no VIN) as an asset under
`assets/`.

Open facts from 01 that only a live key can settle (spend the 20 req/h budget carefully):
rate-limit scope per VIN vs per key (two keys on one VIN), any exempt endpoint; key lifetime
and the 401 `type` for a deleted key; 202 → state latency, asleep-car behaviour, repeated
start; whether `GET /api/v1/vehicles` (no VIN) exists; any terms shown in the app at key
creation. Key lives at `~/.config/missingmcp/skoda-key` (0600, outside the repo).

## Comments

- 2026-10-28 (Vaclav): a key **cannot** be created on the web (go.skoda.eu/api-keys) — only in
  the MyŠkoda **mobile app**: Profil → Chytrá domácnost → Vytvořit klíč → name (here
  "Test MissingMCP") → Kopírovat klíč. Relevant for the connect-page copy (ticket 04).

## Answer

Done 2026-10-28 on Vaclav's car: 7 counted calls + one AC start/stop. Full log, redacted
response shapes and error bodies: [assets/02-live-key-observations.md](../assets/02-live-key-observations.md).

- **Key:** created only in the mobile app; fixed lifetime of **~6 months** (180 days), not
  selectable; max **5 keys per account**. The VIN is copyable on the same app screen. No terms
  shown at creation. Other keys on the car (e.g. a Grok bot) share the budget.
- **Rate limit:** 20/h **per VIN** (docs; worst case adopted, no second-key test). Fixed 1-h
  window from the first counted call; `Reset` counts down. 404 and 403 are not counted.
- **No list endpoint** (`GET /api/v1/vehicles` → 404). The user must supply the VIN.
- **Action latency:** `202` is instant. A sleeping car took **~2 min 13 s** to report the AC
  start; an awake car reported the stop in **~4 s**. A read in between returns stale state; only
  `carCapturedTimestamp` tells you whether a read is fresh. AC auto-stops after 30 min (no
  duration param); a start persists the target temperature and turned window heating on.
- **`operations[]` is per car** (this EV: charging + AC only; aux heat and ventilation are
  `*_UNSUPPORTED`). The tool surface should follow it.
- **Privacy:** a full read includes GPS, address, licence plate, car name and VIN (→ 05).
