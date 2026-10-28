# MyŠkoda Public API: facts as of 2026-10-28

Answers ticket [01](../issues/01-skoda-public-api-research.md). Everything here was fetched live
on 2026-10-28 unless marked otherwise. Labels used below:

- **[doc]**: stated in Škoda's own docs or contract
- **[observed]**: seen in a live, keyless HTTP call
- **[inference]**: my reading; not stated anywhere
- **[needs live key (ticket 02)]**: can only be settled by calling with a real key

The unofficial `myskoda` / `skodaconnect` app API is **not** used as a source here. It is out:
end of life in October 2026.

## Sources

| What | URL | Notes |
| --- | --- | --- |
| Developer docs (landing) | https://public.api.connect.skoda-auto.cz/docs | Public HTML. Covers auth/expiry, partial data, rate limits, errors, personal-use clause. |
| **Curated contract (authoritative)** | https://public.api.connect.skoda-auto.cz/docs/myskoda-public-api.yaml | OpenAPI 3.0.3, `info.version: 1.1.0`. This is what Swagger UI loads (`/v3/api-docs/swagger-config` → `url`). It declares response headers and `securitySchemes`. Saved as [`skoda-public-api-1.1.0.yaml`](skoda-public-api-1.1.0.yaml). |
| Generated OpenAPI | https://public.api.connect.skoda-auto.cz/v3/api-docs | Public, no key needed. OpenAPI 3.1.0, `info.version: v0`, springdoc-generated. Same 12 paths, but it **omits** response headers and security. Saved as [`skoda-openapi-v3.json`](skoda-openapi-v3.json). Prefer the curated YAML. |
| Interactive reference | https://public.api.connect.skoda-auto.cz/docs/swagger-ui.html | |
| Changelog | https://public.api.connect.skoda-auto.cz/docs/changelog | 1.0.0 (first stable) and 1.1.0 (plug state fields). |
| Problem-type pages | `https://public.api.connect.skoda-auto.cz/problems/<type>` | One page per RFC 9457 problem type (listed in Q4–Q6). |
| Key management | https://go.skoda.eu/api-keys | On desktop it 302s to https://www.skoda-auto.com/connectivity/myskoda-app. On a phone it is meant to deep-link into the app. |
| Commercial alternative | https://drivesomethinggreater.com/developer-hub/api-specifications/charging-data-api-specification | Volkswagen Group Info Services (GIS) Charging Data API. |

## 1. Endpoints

Host: `https://public.api.connect.skoda-auto.cz`. Auth on every call: header `X-API-Key`.
There are 12 operations; the `operationId`s match `Vehicle.operations[].name` [doc].

| Method | Path | operationId | Purpose | Read/action | Sync/async |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/v1/vehicles/{vin}` | `getVehicle` | Vehicle and its current state. Optional `?include=` takes a comma list of `info, status, fuelStatus, odometer, parkingPosition, airConditioning, auxiliaryHeating, activeVentilation, charging, chargingProfiles, operations`. | read | sync `200` (may be partial, see below) |
| POST | `/api/v1/vehicles/{vin}/charging/start` | `startCharging` | Start charging | action | `202`, no body |
| POST | `/api/v1/vehicles/{vin}/charging/stop` | `stopCharging` | Stop charging | action | `202`, no body |
| PUT | `/api/v1/vehicles/{vin}/charging/limit` | `setChargingLimit` | Body `{targetStateOfChargeInPercent}`. Schema allows 1–100, but "vehicles typically accept 50–100 in steps of 10". | action | `202` |
| PUT | `/api/v1/vehicles/{vin}/charging/mode` | `setChargeMode` | Body `{chargeMode}`: `MANUAL, TIMER, TIMER_CHARGING_WITH_CLIMATISATION, PREFERRED_CHARGING_TIMES, ONLY_OWN_CURRENT, IMMEDIATE_DISCHARGING, HOME_STORAGE_CHARGING` | action | `202` |
| PUT | `/api/v1/vehicles/{vin}/charging-profiles/{id}` | `updateChargingProfile` | Replace a whole charging profile; send it complete, as read via `include=chargingProfiles`. Returns `412` if the URL id ≠ body id. | action | `202` |
| POST | `/api/v1/vehicles/{vin}/air-conditioning/start` | `startAirConditioning` | Optional body `{targetTemperature:{value,unit CELSIUS/FAHRENHEIT}, airConditioningWithoutExternalPower}` | action | `202` |
| POST | `/api/v1/vehicles/{vin}/air-conditioning/stop` | `stopAirConditioning` | | action | `202` |
| POST | `/api/v1/vehicles/{vin}/auxiliary-heating/start` | `startAuxiliaryHeating` | Body `{spin (REQUIRED), targetTemperature?, durationInSeconds?, startMode HEATING/VENTILATION?}` | action | `202` |
| POST | `/api/v1/vehicles/{vin}/auxiliary-heating/stop` | `stopAuxiliaryHeating` | | action | `202` |
| POST | `/api/v1/vehicles/{vin}/active-ventilation/start` | `startActiveVentilation` | No body | action | `202` |
| POST | `/api/v1/vehicles/{vin}/active-ventilation/stop` | `stopActiveVentilation` | | action | `202` |

What `getVehicle` returns, by part [doc, schemas in the YAML]:

| Part | Contents |
| --- | --- |
| `info` | `name` (user's name for the car, falling back to the model name), `licensePlate`, `renderUrl` |
| `status` | doors, windows and lights; `overall.doorsLocked` etc. |
| `fuelStatus` | `carType`, ranges, AdBlue |
| `odometer` | `mileageInKm` |
| `parkingPosition` | `state` `PARKED` / `IN_MOTION`, `gpsCoordinates`, `formattedAddress` |
| `airConditioning` | `state` `OFF`/`COOLING`/`HEATING`/`HEATING_AUXILIARY`/`VENTILATION`/`COMPLETED`/…, target temperature, ETA, window heating |
| `auxiliaryHeating`, `activeVentilation` | state, duration |
| `charging` | `battery.stateOfChargeInPercent`, `remainingCruisingRangeInMeters`. `status`: `state`, `chargePowerInKw`, `chargeType`, `remainingTimeToFullyChargedInMinutes`, and since 1.1.0 `plugConnectionState` / `plugLockState`. Plus settings and `isVehicleInSavedLocation`. |
| `chargingProfiles` | saved charging locations |
| `operations` | the remote operations this vehicle supports |

Most parts carry a `carCapturedTimestamp`.

**Partial data [doc]:** a `200` can omit parts. Each omitted part gets an `errors[]` entry, typed
`<PART>_UNSUPPORTED|_DISABLED|_UNAVAILABLE`, `RENDER_UNAVAILABLE` or `OPERATIONS_UNAVAILABLE`.
`*_UNSUPPORTED` entries appear only for parts explicitly requested via `include`. A missing field
*inside* a part has no `errors[]` entry; the docs say to treat it as "unknown, never as a
specific value".

**Not in the API [doc, by absence from the contract]:**

- lock/unlock, honk/flash, windows, trips/driving statistics, service/maintenance
- **any list-vehicles or "who am I" endpoint**: `GET /api/v1/vehicles` without a VIN is not in
  the contract. Keyless, it returns `401` [observed], so whether the route exists at all is
  unknown.

The seed fact "no remote lock / honk" is confirmed.

## 2. Key scope

- [doc] "Keys are bound to the vehicles you selected when creating them". So a key covers a
  user-chosen **set of VINs**, possibly several. It is not bound to the user's whole garage, and
  not necessarily to exactly one VIN. A VIN outside that set → `403 api-key-not-authorized`.
- [doc, by absence] **The API cannot tell a client which VINs a key covers.** There is no
  list endpoint, and every path takes `{vin}`. The client has to be told the VIN(s): the user
  types them in, or copies them from the app.
- [inference] The connector's connect form must ask for **key + VIN(s)**. It can then validate
  each VIN with `GET /api/v1/vehicles/{vin}?include=info`: `200` means covered, `403` means not
  covered, `404` means unknown VIN. Each check costs 1 of that VIN's ~20/h quota on success;
  401/403 don't consume quota [doc].
- "Max ~5 keys per VIN" is **not** in Škoda's docs. It comes only from secondary coverage
  ([mobiflip](https://www.mobiflip.de/skoda-oeffnet-seine-fahrzeugdaten-fuer-home-assistant-und-co/)
  says "up to five access tokens per chassis number"). Check it in the app → **needs live key
  (ticket 02)**.
- How long a key lives, and whether its VIN set can be edited later: the docs say "create **or
  update** a key in the MyŠkoda app", which suggests keys can be edited. Whether an updated key
  keeps the same secret → **needs live key (ticket 02)**.

## 3. Identity

- [doc, by absence] **No endpoint returns any user identity**: no email, no user id, no profile.
  The only identifiers anywhere in the contract are `vin`, `name` and `licensePlate`.
- [doc] Keys *are* user-bound internally. `operation-not-authorized` reads "the vehicle refused
  the operation for the user the API key belongs to". That user is never exposed.
- [inference] The `account_key` cannot be the login email the way Garmin and WHOOP do it. The
  candidates are:
  - the VIN (or a hash of it), which is stable and verifiable via `getVehicle`
  - a hash of the key, which is unstable because keys rotate
  - an email the user types in, which cannot be verified

  This is for ticket 04 to decide.

## 4. Expiry

- [doc] "Keys … expire." The lifetime and the renewal procedure are **not documented**. You
  renew only by creating or updating a key in the app; there is no API to rotate a key.
  Default lifetime and options → **needs live key (ticket 02)**.
- [doc] `X-API-Key-Expires-At` is sent on **every successful response** (`200`/`202`) and is
  also declared on the `401` response. It is an RFC 3339 date-time, e.g.
  `2026-09-01T00:00:00Z`; "rotate the key before this time".
- [doc] An expired key gets `401` with
  `type: https://public.api.connect.skoda-auto.cz/problems/api-key-expired`, for example:

  ```json
  {"type":"https://public.api.connect.skoda-auto.cz/problems/api-key-expired",
   "title":"Unauthorized","status":401,"detail":"The API key expired on 2026-09-01T00:00:00Z."}
  ```

- [observed] A missing or invalid key gets `401` with `application/problem+json`:

  ```json
  {"detail":"Missing or invalid API key.","instance":"/api/v1/vehicles/<vin>","status":401,"title":"Unauthorized","type":"about:blank"}
  ```

- [doc] Revocation: "Losing access to a vehicle … revokes the keys bound to it, so those answer
  401." The `type` a revoked key carries (`about:blank` or `api-key-expired`) → **needs live key
  (ticket 02)**. A key deleted in the app presumably looks like "invalid" (`about:blank`)
  [inference].
- [doc] `403 api-key-not-authorized` means the key is valid but this VIN or operation is not
  covered. The fix is to edit or create a key in the app.
- [doc] `503` means "the API key could not be validated" (a transient upstream problem) and
  carries `Retry-After`. It must **not** be treated as re-auth.
- [inference] For re-auth mapping:

  | Response | Meaning | Handling |
  | --- | --- | --- |
  | any `401` | key expired, invalid or revoked | re-auth |
  | `403 api-key-not-authorized` | key lacks this VIN | tell the user to fix the key in the app; re-auth with a new key |
  | `403 operation-not-authorized`, `422`, `429`, `503` | not a key problem | not re-auth |

## 5. Rate limits

- [doc, landing page] "Requests are rate-limited **per VIN**. The current limit is **20
  requests per hour**; this value is not final and may change. The response headers are the
  authoritative source."
- [doc, problem page `rate-limit-exceeded`] "The quota this API grants **per vehicle**".
- **Contradiction in Škoda's own docs:**
  - Changelog 1.0.0 says "rate-limited **per key**".
  - The `ProblemDetail` enum and the `429` response text say "the rate limit **for the API
    key** has been exceeded".

  The newest and most specific texts (landing page, problem page) say per VIN. Possibly the
  quota is per (key, VIN), or per VIN shared across all keys → **needs live key (ticket 02)**.
  Test it with two keys on the same VIN.
- [doc] The headers are `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`. Reset
  is **seconds until the window replenishes**, per the IETF RateLimit draft.
- [doc] They are present on every rate-limited response: success, error, and `429`.
- [doc] Over the limit the API returns `429` with `Retry-After`, an integer number of seconds
  that marks the end of the current window.
- [doc] "**Every authenticated request to a rate-limited endpoint counts** against the quota,
  including requests that end with an error response (e.g. a 5xx) … Requests rejected with 401
  or 403 do not consume quota, and requests rejected with 429 do not prolong the current
  window."
- [inference] Reads and actions **share one budget**: nothing separates them, and every one of
  the 12 operations declares the `RateLimit-*` headers. The phrase "rate-limited endpoint"
  hints that some endpoints may be exempt, but all 12 declare the headers → confirm with
  **needs live key (ticket 02)**.
- [doc] There is a **second, independent limit inside the car**: `429
  vehicle-not-accepting-requests`. It covers how often the car executes requests, a low 12 V
  battery, or an operation already in progress, and it has its own `Retry-After` (example 10
  s). The `RateLimit-Remaining: 0` header can't tell the two 429s apart; only `type` can.
- [inference] At ~20/h, **polling to confirm an action is expensive**. Start AC plus three
  status polls is 4 of 20. The tool design should avoid auto-polling and serve recent state from
  a short cache.

## 6. Action semantics

- [doc] Every action returns **`202`** ("Request to … was performed") with **no body**, **no
  `Location` header and no operation id**, and **there is no operation-status endpoint**. The
  only way to see the effect is to re-read `getVehicle` and compare the state field and
  `carCapturedTimestamp`.
- [doc] Calls can also fail with `504 Gateway Timeout`, e.g. "The vehicle did not respond in
  time". [inference] So the `202` may be returned only after the car (or its backend)
  acknowledges, i.e. the call is synchronous up to acknowledgement and asynchronous for the
  actual effect. How long a `202` / `504` takes → **needs live key (ticket 02)**.
- Errors, all `application/problem+json` per RFC 9457 [doc]:

  | Status | Type | Meaning | Retry? |
  | --- | --- | --- | --- |
  | 400 | `about:blank` | Malformed body or unknown `include`. Extension members `parameter`, `rejectedValue`, `allowedValues`. | no |
  | 401 | `api-key-expired` / `about:blank` | Key expired, invalid or revoked | re-auth |
  | 403 | `api-key-not-authorized` | VIN not covered by the key | no; fix key |
  | 403 | `operation-not-authorized` | Car refused for this user. Rare race; retrying won't help. | no |
  | 404 | `about:blank` | Unknown VIN (or unknown charging profile id) | no |
  | 412 | `about:blank` | Charging profile id mismatch | no |
  | 422 | `operation-not-supported` | Car lacks the capability (e.g. charging on an ICE car) | never |
  | 422 | `operation-disabled` | Capability present but disabled: consent revoked, connectivity licence expired, workshop mode | not until fixed in app |
  | 429 | `rate-limit-exceeded` | API quota used up | after `Retry-After` |
  | 429 | `vehicle-not-accepting-requests` | Car declined: own throttle, low battery, op in progress | after `Retry-After` |
  | 500 | `about:blank` | Internal | maybe |
  | 503 | `about:blank` | e.g. key validation unavailable; `Retry-After` | yes |
  | 504 | `about:blank` | Vehicle did not respond in time | maybe |

- **"Car asleep / offline":** there is no dedicated problem type [doc]. [inference] It most
  likely surfaces as `504` or as `vehicle-not-accepting-requests`, and for reads as
  `*_UNAVAILABLE` entries in `errors[]` → **needs live key (ticket 02)**.
- **Capability gating up front [doc]:** `getVehicle` lists the supported operations in
  `operations[]`. An operation "may still be temporarily rejected when executed (e.g. workshop
  mode)". This list is a natural source for which tools are available per car [inference].
- **Auxiliary heating needs the S-PIN [doc]:** `startAuxiliaryHeating` requires a `spin`
  ("Security PIN code") in its body. [inference] The connector would have to collect the
  user's S-PIN, either per call from the user in chat or stored in the encrypted blob. That is
  a security and UX decision for ticket 05. AC start and ventilation start need no PIN.
- **Idempotency:** nothing is documented. Whether starting AC while it's already running gives
  `202`, `429 vehicle-not-accepting-requests` ("an operation already in progress") or something
  else → **needs live key (ticket 02)**.

## 7. ToS

- [doc, landing page, verbatim] "**Personal use only.** The MyŠkoda Public API is provided for
  personal end-user use only. The API may not be used to develop, provide, or operate commercial
  products or services, nor may it be resold, redistributed, or otherwise exploited for
  commercial purposes. If you require API access for commercial use cases, please refer to our
  official Charging Data API available via the Developer Hub."
- [doc] The same page frames the API as for "third-party developers … for example to connect a
  car to a smart-home platform such as Home Assistant". Third-party *integrations* are the
  intended use.
- **Not found on the public docs:**
  - any wording about **storing keys on someone else's behalf**
  - any wording about hosted multi-user services
  - any wording about sharing keys
  - a separate terms-of-use page linked from the docs

  Any in-app terms shown when a key is created → **needs live key / app (ticket 02)**: read and
  capture them.
- [inference] A free, non-commercial hosted relay where each user brings their own key is not
  explicitly forbidden. It is not explicitly allowed either. The word "operate … commercial …
  services" is the line, so this matches the map's "no monetization, no promotion" stance.
- Commercial route [doc]: the VW Group Info Services **Charging Data API**
  ([spec](https://drivesomethinggreater.com/developer-hub/api-specifications/charging-data-api-specification)).
  - It requires onboarding in the GIS Data Hub and "purchase a subscription".
  - It uses its own header, `x-marketplace-api-key`.
  - It covers **charging data only**; climate is not mentioned.
  - The `gis-sales@cariad.technology` address from the seed facts does **not** appear on that
    page, which links only to a contact-us form. Its source is unverified.

## 8. VW Group: other brands

**Verdict as of 2026-10-28: no equivalent official owner-facing public API found for VW,
Audi, Cupra or Seat.** Škoda appears to be the only group brand that ships one.

- [observed] Hosts following Škoda's pattern do not resolve or answer. Probed:
  `public.api.connect.{volkswagen.de, volkswagen.com, vw.com, audi.com, audi.de, cupra.com,
  cupraofficial.com, seat.com}`. This is weak evidence on its own, because the hosts are guesses.
- [primary] The only group-level official API offering is GIS
  ([Developer Hub](https://drivesomethinggreater.com/developer-hub)). It is B2B, subscription
  based, and offers Charging Data, Fleet Interface Data and similar products. It also has an
  EU Data Act section (https://drivesomethinggreater.com/content/gis/de/en/eu-data-act), which
  is a data-access portal, not a control API.
  - The GIS newsroom has no 2026 item about an owner API, only partnerships (Smartcar
    2026-04-30, Yellowfox and Samsara 2026-10-01).
- [secondary, flagged as such] The group shut down third-party access to the old brand-app API
  for VW, Audi, Škoda and Cupra. It announced this on 2026-04-02 ("from calendar week 21"), and
  breakage was observed from 2026-05-27. VW said it has been "in exchange since autumn 2025"
  with the open-source community about a joint solution but has named nothing concrete.
  Sources: heise
  [11312776](https://heise.de/-11312776) and [11326538](https://heise.de/-11326538), and
  [electrive 2026-06-01](https://www.electrive.net/2026/06/01/vw-sperrt-api-fuer-externe-ladesteuerung/).
  I could not reach VW's own 2026-04-02 notice.
- [secondary] On the Škoda API's launch:
  - [evcc blog, 2026-08-27](https://docs.evcc.io/en/blog/2026/08/27/skoda-vehicle-api) says it
    shipped with MyŠkoda app 8.16, was co-designed with the HA / skodaconnect developers and the
    Open Home Foundation, and that talks with "the VW brand for about a year now" have produced
    "so far no concrete result".
  - No Škoda Storyboard press release for the Public API was found.
- [inference] For the spec: design the Škoda slice as one upstream behind a seam. Do not assume
  the other brands will share this host, auth or shape, because none exists to compare against
  yet. Re-check before the next-OEM slice.

## Seed facts: re-check result

| Seed | Live result |
| --- | --- |
| Host, `/v3/api-docs` | ✅ Both public, no key needed. But the **curated** contract is `/docs/myskoda-public-api.yaml` (v1.1.0); `/v3/api-docs` is a generated v0 that lacks headers and security. |
| Auth `X-API-Key` from app, go.skoda.eu/api-keys | ✅ |
| `X-API-Key-Expires-At`; keys expire | ✅. Lifetime is undocumented. |
| Max ~5 tokens per VIN | ⚠️ Not in Škoda's docs (secondary source only) → ticket 02 |
| ~20 req/h per VIN, `RateLimit-*`, `429` + `Retry-After` | ✅ per landing page, but ⚠️ the docs contradict themselves on per-VIN vs per-key. There is also a separate car-side 429. |
| AC start/stop, aux heating, ventilation | ✅. ⚠️ Aux heating **requires the S-PIN**. |
| Charging / SoC | ✅, plus charge limit, charge mode and charging-profile update |
| No remote lock / honk | ✅ |
| ToS personal use only; commercial → Charging Data | ✅ wording confirmed. `gis-sales@cariad.technology` is not found on the linked page. |

## Open for ticket 02 (live key)

1. Is the rate limit per VIN, per key, or per (key, VIN)? Do reads and actions share it? Are
   any endpoints exempt?
2. Default key lifetime; is it configurable; does "update key" keep the secret; max keys per VIN.
3. The `401` `type` for a revoked key vs a deleted key.
4. `202` / `504` latency, behaviour when the car is asleep, and idempotency of a repeated start.
5. Any ToS / consent text shown in the app when a key is created.
6. Whether `GET /api/v1/vehicles` (no VIN) exists for a valid key.
