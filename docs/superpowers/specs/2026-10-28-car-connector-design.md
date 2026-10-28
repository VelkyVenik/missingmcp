# Car Connector Design (Škoda first)

**Date:** 2026-10-28
**Status:** approved design. Every decision comes from a resolved wayfinder ticket under
`.scratch/car-connector/` (map: `.scratch/car-connector/map.md`), worked with the operator.
**Branch:** feat/car-connector (worktree)

## Goal

A generic **`car` connector** for MissingMCP: one URL (`/car/mcp`) that a Claude team registers
once, with the car **brand** chosen per account at sign-in. The first and so far only brand is
**Škoda**, through the official **MyŠkoda Public API**. A person pastes their own API key and VIN,
then asks Claude about the car (battery, range, locks, climate) and starts or stops climate,
ventilation and charging. More brands will follow when they publish an owner API; the design
leaves that seam open without building for a brand we can't see yet.

Vocabulary (`CONTEXT.md`): **connector** `car`, served by the **adapter** `car`. A **brand** decides
an account's **upstream**: the Škoda Public API is the `skoda` brand's upstream.

## Research summary (2026-10)

Full detail lives in the tickets' assets. This section only gives the facts the design stands on.

- **Upstream:** `https://public.api.connect.skoda-auto.cz`. The authoritative contract is
  `/docs/myskoda-public-api.yaml` (v1.1.0); `/v3/api-docs` is an incomplete generated variant.
  It has one read, `GET /api/v1/vehicles/{vin}?include=…`, and 11 actions (charging start/stop,
  limit, mode, profile; AC, aux heating and ventilation start/stop). There is no lock/honk and no
  list-vehicles endpoint (a live call returns 404). See
  [Škoda Public API + VW Group facts](../../../.scratch/car-connector/issues/01-skoda-public-api-research.md).
- **Auth:** an `X-API-Key` header. Keys are created **only in the MyŠkoda mobile app** (Profil →
  Chytrá domácnost → Vytvořit klíč) and bound to vehicles picked at creation. Lifetime is fixed at
  **~6 months**; every success carries `X-API-Key-Expires-At`. An account can have at most 5 keys.
  The VIN is copyable on the same app screen. The API returns **no user identity**.
- **Errors:** 401 `api-key-expired` or `about:blank` (invalid key); 403
  `api-key-not-authorized` (VIN not covered); 422 `operation-not-supported` /
  `operation-disabled`; 429 quota vs 429 `vehicle-not-accepting-requests` (told apart only by
  `type`); 504 (the car didn't respond).
- **Rate limit:** **20 requests/hour per VIN**, shared by all keys on that car, in a fixed window
  from the first counted call. `RateLimit-Limit/Remaining/Reset` headers; `Retry-After` on 429.
  Observed live: 200 and 202 count, 403 and 404 don't.
- **Actions:** `202` with no body and no operation id; confirming means re-reading. Live: a
  sleeping car took **~2 min 13 s** to reflect an AC start, an awake car **~4 s** for the stop.
  Freshness shows only in each section's `carCapturedTimestamp`. AC auto-stops after 30 min.
  Aux-heating start requires the user's **S-PIN**. See
  [Live key against own Škoda](../../../.scratch/car-connector/issues/02-live-key-smoke.md).
- **Per car:** `operations[]` lists the actions this vehicle supports. The operator's EV supports
  charging + AC only; aux heating and ventilation come back as `*_UNSUPPORTED`.
- **Privacy:** a full read includes GPS coordinates, the street address, licence plate, car name
  and VIN.
- **ToS:** "personal end-user use only … may not be used to develop, provide, or operate
  commercial products or services." Nothing is said about relays that hold keys on someone's
  behalf.
- **Other brands:** no official owner API exists for VW, Audi, Cupra/Seat or Porsche, and none is
  announced with a date. VW closed its brand-app API to third parties in CW21 2026. See
  [VW Group owner API availability](../../../.scratch/car-connector/issues/07-vw-owner-api.md).

## Decisions

1. **One generic connector `/car`**, adapter `car`, with the brand picked on the authorize form.
   Reason: Claude registers team connectors per team, and the operator wants exactly one for cars.
   A brand that can't fit `/car` later gets its own adapter `car-<brand>`.
   ([Connector shape and slug](../../../.scratch/car-connector/issues/03-connector-shape-slug.md))
2. **v1: one account = one brand + one VIN.** Several VINs or several brands per account is a
   later blob extension.
3. **`account_key` = the email typed on the form**, normalized via `base.normalize_account_key`,
   **unverified**. The system already treats `account_key` as an email (campaigns, PostHog
   `distinct_id`), and the upstream offers no identity.
   ([BYO-key auth, account_key, expiry](../../../.scratch/car-connector/issues/04-auth-account-key.md))
4. **The S-PIN is never collected or stored**, so aux heating is not in v1.
5. **Brand-neutral tools** filtered per car by `operations[]`, plus a capabilities tool. Location
   is only in its own tool. Actions return at once with no gateway polling.
   ([v1 tool set and rate-limit handling](../../../.scratch/car-connector/issues/05-tool-set-rate-limits.md))
6. **Hosted beta on missingmcp.com**, free. Each user brings their own key. Prominent ToS/risk copy.
   It ships **enabled with a beta label**: no env kill switch and no operator secret. The page is
   not on the homepage, not in campaigns and not in MCP directories (Garmin stance: no
   monetization, no promotion).
7. **In-tree, in-process MCP on the local forward strategy**, reusing whoop's hand-rolled
   JSON-RPC layer pattern. There is no worker, no community server and no token refresh: the key
   is static until it expires.

## Architecture

### Login: form login, one network call

The `car` adapter uses the existing **form-login** seam (like garmin, no second factor, no
upstream OAuth).

- **Authorize form** (`templates/car_authorize.html`, rendered on `GET /car/oauth/authorize`):
  1. **Brand**: a select, only "Škoda" today. Fields below it are brand-specific (server-rendered;
     with a single brand, no JS is needed).
  2. **Email**: "used only for important news about this connector".
  3. **API key**: a password-type input.
  4. **VIN**: 17 characters, uppercased and stripped server-side.

  Setup copy: "In the MyŠkoda app: Profil → Chytrá domácnost → Vytvořit klíč → Kopírovat klíč.
  The VIN is on the same screen." Add a screenshot, and a short ToS note (personal use; the key
  is stored encrypted and can be revoked in the app any time).
- **`login_hint(form)`** returns the typed email (for the `login-start` log line, hashed as usual).
- **`start_login(form)`** does the **only upstream call** of a sign-in:
  `GET /api/v1/vehicles/{vin}?include=operations` with the key. It costs 1 of the 20/h.
  - 200 → `LoginOk(account_key=normalize_account_key(email), blob=…)`. The blob captures
    `expires_at` (from `X-API-Key-Expires-At`) and `operations[]`. (`info` holds only the
    user-defined name, plate and render URL. The API has no model field, so `info` isn't
    requested.)
  - 401 → `LoginError("That API key isn't valid or has expired — create a new one in MyŠkoda.")`
  - 403 → `LoginError("This key doesn't cover that VIN — check the VIN, or create the key for this car.")`
  - 429 → `LoginError("Škoda's limit of 20 requests/hour for this car is used up — try again in N min.")`
    (N from `Retry-After`).
  - Timeout, 5xx or network failure → a generic "Škoda didn't answer, try again" `LoginError`.
  - Form validation (bad email, VIN not 17 chars, empty key) fails before the network call.
- **`verify(blob)`** makes **no network call**. It decodes the blob, checks its shape (brand,
  key, VIN, `expires_at` present and not in the past) and returns a display name for logging
  (`"Škoda"`; the user-defined car name stays out of logs). The upstream check has already happened in `start_login`,
  in the same authorize request; a second GET would spend another unit of a 20/h budget for
  nothing. Verify-then-persist still holds: `_finish` runs only after both succeeded.
- Both calls go through `_bounded` as every adapter sign-in must (they're synchronous httpx).
- `resume_second_factor` is never reached (`start_login` never returns `SecondFactorNeeded`).

### Blob

Encrypted JSON (AES-256-GCM like every blob), versioned for the later multi-VIN / multi-brand
extension:

```json
{"v": 1, "brand": "skoda", "api_key": "…", "vin": "TMB…", "expires_at": "2027-04-26T09:11:23Z",
 "operations": ["startAirConditioning", "stopAirConditioning", "startCharging", "…"]}
```

The API key and VIN are **never logged** and never materialized to files. Logs carry at most the
8-char account-key hash prefix (as today). The VIN never leaves the blob except in upstream URLs.

### Forward: `LocalForward` (whoop pattern)

`CarLocalForward.handle(conn, account_key, blob, body)` runs the MCP server in-process.

- **Stateless:** `GET`/`DELETE /car/mcp` → 405. No WorkerManager.
- **Per brand dispatch:** `blob["brand"]` selects the brand client (`adapters/car/skoda.py`).
  Tools call a small brand-client interface: `read(sections)`, `action(name, body)`. A second
  brand adds a module that implements it. A shared base module is extracted only then.
- **Rotation:** none. The only blob write after sign-in is refreshing `operations[]`
  when a status read returns a different list. It is persisted via the store in the same request.
- **401 from the upstream on any tool call** (expired, revoked, or deleted key) →
  `SessionExpired` → the proxy's existing re-auth 401 (`_reauth_required`). The client re-runs
  authorization and the person pastes a fresh key.

### MCP protocol layer

The same stateless JSON-RPC subset as whoop (`initialize`, `notifications/*`, `tools/list`,
`tools/call`, `ping`). Responses are `application/json` with no session id. Tool failures are
results with `isError: true`. Whether to share code with `whoop/mcp.py` or copy it is decided at
planning time; prefer extracting a small shared helper if it falls out cleanly.

- **`tools/list` is per account:** only tools whose upstream operation is in the blob's
  `operations[]`, plus the read tools that are always available. This costs no upstream call
  (the list comes from the blob).
- Tool annotations: read tools get `readOnlyHint: true`; action tools get `readOnlyHint: false`
  so MCP clients ask before running them.

### Tools (v1)

| Tool | Upstream | Shown when | Cost |
|---|---|---|---|
| `get_vehicle_capabilities` | none (blob + in-memory budget) | always | 0 |
| `get_vehicle_status` | `GET ?include=status,charging,airConditioning,activeVentilation,odometer,operations` | always | 1 (0 if cached) |
| `get_vehicle_location` | `GET ?include=parkingPosition` | always | 1 |
| `start_climate(target_temperature_c?, allow_battery?)` | `POST …/air-conditioning/start` (`allow_battery` → `airConditioningWithoutExternalPower`) | `startAirConditioning` | 1 |
| `stop_climate` | `POST …/air-conditioning/stop` | `stopAirConditioning` | 1 |
| `start_ventilation` / `stop_ventilation` | `POST …/active-ventilation/start\|stop` | `startActiveVentilation` / `stopActiveVentilation` | 1 |
| `start_charging` / `stop_charging` | `POST …/charging/start\|stop` | `startCharging` / `stopCharging` | 1 |
| `set_charge_limit(percent)` | `PUT …/charging/limit` | `setChargingLimit` | 1 |

- **`get_vehicle_capabilities`** returns the brand, VIN (last 6 chars), supported actions
  in words, key `expires_at`, and the last known budget.
- **`get_vehicle_status`** output uses brand-neutral English keys, metric units, °C and UTC ISO
  timestamps. Each section has its own `captured_at`. Example shape:
  `{"vehicle": {"brand": "skoda", "vin_tail": "123456"}, "locks": {...},
  "doors": {...}, "windows": "closed", "battery_percent": 75, "range_km": 296,
  "charging": {"state": "not_connected", "power_kw": 0, "limit_percent": 80, "captured_at": "…"},
  "climate": {"state": "off", "target_c": 22, "captured_at": "…"}, "odometer_km": 8279,
  "budget": {...}}`. It **never** includes GPS, address, licence plate, car name or the full VIN.
- **`get_vehicle_location`** returns `{"parked": true, "address": "…", "lat": …, "lon": …,
  "captured_at": "…"}`. Its description tells the model to call it only when the user asks where
  the car is.
- **`set_charge_limit`** accepts 50–100 in steps of 10 and rejects anything else locally.
  `start_climate` validates `target_temperature_c` against the upstream schema range at planning
  time.
- **Action results** look like `{"accepted": true, "message": "Command sent. A sleeping car can
  take ~2 minutes to react — check with get_vehicle_status later.", "budget": {...}}`. The
  gateway doesn't wait or poll, and repeated starts are passed through as-is.
- **Not in v1:** charge mode, charging profiles and timers, auxiliary heating.

### Rate-limit handling

- Every tool result carries `budget: {"remaining": n, "resets_in_min": m}` from the last
  `RateLimit-*` headers. The last value is kept **in memory per VIN** (process-local, like the
  other limiters; single-node by design) so `get_vehicle_capabilities` can report it at no cost.
- **60 s status cache** per account (in memory): a second `get_vehicle_status` within 60 s
  returns the cached result with `cached: true`. Actions invalidate the cache.
- No gateway-side throttle below the upstream's 20/h. The upstream decides.
- Error mapping (all as MCP tool errors, never re-auth):

  | Upstream | Tool error text (gist) |
  |---|---|
  | 429, quota | "Škoda's limit of 20 requests/hour for this car is used up; it resets in N min." |
  | 429 `vehicle-not-accepting-requests` | "The car isn't accepting commands right now (low battery or another operation running)." |
  | 422 `operation-not-supported` / `operation-disabled` | "This car doesn't support that, or it's disabled (consent, licence, workshop mode)." |
  | 504 | "The car didn't respond in time; try again." |
  | 5xx / network | "Škoda's service didn't answer; try again later." |
  | 401 | `SessionExpired` → re-auth 401 (see Forward) |

### Key-expiry notice

From **14 days before `expires_at`**, every tool result carries
`"notice": "Your Škoda API key expires on <date>. Create a new one in the MyŠkoda app (Profil →
Chytrá domácnost) and reconnect."`. After expiry, the upstream's 401 triggers re-auth. Email
reminders are a later addition.

### Configuration

- `CAR_SKODA_API_BASE` (default `https://public.api.connect.skoda-auto.cz`) overrides the base
  for tests and staging.
- **No enable flag and no secret:** `build_adapters` always registers `car`. (It is not a
  retirement-list concern either: the adapter is always present.)

### Files

```
src/missingmcp/adapters/car/
  __init__.py   # CarAdapter (form login, verify, templates) + CarLocalForward wiring
  skoda.py      # Škoda brand client: httpx GET/POST/PUT, header parsing (budget, expiry),
                # error → domain mapping, response normalization to the brand-neutral shape
  mcp.py        # JSON-RPC handler + tool table (name, description, inputSchema, operation,
                # readOnly, fn), per-account filtering, cache, budget, expiry notice
src/missingmcp/templates/car_authorize.html
src/missingmcp/templates/car.html          # landing fragment (_layout)
scripts/gen_car_tools.py                   # regenerates the "All tools" block (gen_whoop_tools pattern)
```

Also touched: registration in `adapters/__init__.py`, and `{USAGE_METER_CAR}` on the landing
page, which `app.py`/`usage.py` handle generically. README gets the connector, the base-URL env
var and the setup steps. CLAUDE.md gets a short invariant entry (see Security). The homepage is
unchanged.

### Landing page `/car`

It follows the connector-page skeleton (`garmin.html` header comment): hero with a **beta pill**
and the server URL `{PUBLIC_URL}/car/mcp` → "What is this?" → **Supported brands** list (per
brand: status, what works, setup steps with the app path, links to the brand's API docs; Škoda is
the only "available" entry, and other brands are left out until they exist) → "What Claude can
do" cards (status, climate, charging, location) → "How to connect" → "Tips & tricks" (e.g.
"Warm the car to 21 °C", "How much range do I have?") → "Limits" (20 requests/hour per car shared
with your other keys; a sleeping car takes ~2 min to react) → **Terms & risks** (Škoda's API is
for personal use; MissingMCP stores your key encrypted; revoke it in the app any time; unofficial
beta) → "Under the hood" → "All tools" (generated) → CTA.

## Security invariants (unchanged plus new ones)

- The API key and VIN live **only** in the encrypted blob. They are never logged, never in
  telemetry, never in files. Form contents never reach logs or PostHog (existing rule).
- The S-PIN is never asked for or stored.
- Location data leaves the gateway only through `get_vehicle_location`. Status output never
  contains location, plate, car name or the full VIN.
- Sign-in calls go through `_bounded`. Verify-then-persist holds (`_finish` only after
  `start_login` and `verify` succeed).
- The typed email is unverified. It is still the `account_key`, so global unsubscribe and every
  existing per-account op apply unchanged.
- Log events (stable schema; amended at implementation): sign-in failures use the existing
  generic `login-start-failed` / `login-verify-failed` events with car `reason` values
  `invalid_form`, `invalid_key`, `vin_not_covered`, `rate_limited`, `upstream_error`. The expired
  key on a tool call surfaces as the existing `local-forward-auth-stale`. One new event:
  `car-upstream-error` (`account`, `tool`, `status`, `problem` = problem-type tail). `account` is
  the account key, as in every other event. Key and VIN never appear.
- **Client-neutral** (operator, 2026-10-28): the connector targets Claude, ChatGPT and Grok
  alike. Server-side it already is (open DCR, standard OAuth 2.1 + MCP). Copy on the form and the
  landing page names all three, and the landing page has connect steps for each.

## Testing

- No real Škoda in the suite. A **fake Škoda upstream** in `tests/conftest.py` (`fake_skoda`,
  pattern: `fake_whoop`) serves `GET /api/v1/vehicles/{vin}` with `include` handling and the
  action endpoints. It emits `RateLimit-*` and `X-API-Key-Expires-At` and can be scripted to
  return 401/403/404/422/429 (both types)/504. `CAR_SKODA_API_BASE` points at it.
- Coverage:
  - authorize form → `start_login` (200 → persist + code + redirect; 401/403/429/timeout →
    re-render with the right copy; form validation before the network);
  - `verify` without a network call;
  - MCP handshake;
  - `tools/list` filtered by `operations[]` (an EV without ventilation never lists it);
  - each tool against the fake;
  - status output contains no location, plate, name or full VIN;
  - 60 s cache (second call makes no upstream hit) and invalidation after an action;
  - `budget` from headers;
  - every error mapping;
  - an upstream 401 on a tool call → re-auth 401;
  - the expiry notice from 14 days out;
  - 405 on GET/DELETE;
  - path-scoped discovery documents for `car`;
  - usage metrics rows.
- **Release gate:** a manual e2e on the operator's real Škoda, sparing the 20/h. Connect via
  Claude (discovery → DCR → form → token), `get_vehicle_capabilities`, `get_vehicle_status`,
  one `start_climate` + `stop_climate` with a status check in between, `get_vehicle_location`
  once. Live findings to compare: [assets/02-live-key-observations.md](../../../.scratch/car-connector/assets/02-live-key-observations.md).

## Out of scope

- Charge mode, charging profiles and timers, auxiliary heating (S-PIN): later tools.
- Several VINs or several brands per account; email verification; an email reminder before key
  expiry.
- Other brands: none have an official owner API today (watch list in the VW ticket's asset).
- The homepage sport/car sections restructure: a separate effort.
- Intent→auto (Auta bot morning preconditioning), which consumes this connector.
- GIS / Charging Data B2B contract; unofficial `myskoda`/`skodaconnect` app API (EOL).
