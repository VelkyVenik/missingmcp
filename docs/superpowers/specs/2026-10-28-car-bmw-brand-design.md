# Car connector: the `bmw` brand (BMW + Mini, BMW CarData) — design

Date: 2026-10-28. Extends [the car connector design](2026-10-28-car-connector-design.md).
Research: `.scratch/car-connector/issues/08` + `assets/08-other-brands-owner-api.md` §1,
and the operator's own working CarData setup (curl/Python, i3, 2026-10-27).

## Goal

A BMW or Mini owner connects their car to `/car` the same way a Škoda owner does — on the
car maker's **official** owner API, with a credential the owner creates themselves — and gets
the read-only tools: capabilities, status, location. CarData can't send commands, so the
action tools never show for a BMW.

## Upstream facts (CarData customer API)

Labels: **[bmw]** = BMW's own OpenAPI files (found in the portal bundle, copied to
`.scratch/car-connector/assets/bmw-cardata-*.json`); **[op]** = seen in the operator's working
setup; **[evcc]** = evcc's Go client; **[?]** = to confirm in the live test.

- **Credential:** the owner creates a **Client ID** (UUID) once in the MyBMW web portal
  (BMW CarData → Technical access) and ticks *CarData API*. It is a public OAuth client: no
  secret. **[op]**
- **Sign-in:** OAuth **device authorization grant + PKCE S256**. **[op]**
  - `POST https://customer.bmwgroup.com/gcdm/oauth/device/code` with `client_id`,
    `response_type=device_code`, `scope=authenticate_user openid cardata:api:read`,
    `code_challenge`, `code_challenge_method=S256` → `device_code`, `user_code`,
    `verification_uri` (`https://customer.bmwgroup.com/oneid/link` — the user types the code
    there; no `_complete` variant), `expires_in` (300), `interval`. **[bmw]**
  - `POST https://customer.bmwgroup.com/gcdm/oauth/token` with
    `grant_type=urn:ietf:params:oauth:grant-type:device_code`, `device_code`, `client_id`,
    `code_verifier` → `access_token`, `refresh_token`, `id_token`, `gcid`, `expires_in` (3599).
    Errors: 403 `authorization_pending` / `access_denied`, 401 `expired_token` /
    `invalid_client`, 400 `slow_down`. **[bmw]**
- **Tokens:** access token ~1 h; refresh via `grant_type=refresh_token` + `client_id`;
  the response carries a **new refresh token** (treat as rotating, like WHOOP); the refresh
  token lives ~2 weeks. **[op]**
- **REST:** base `https://api-cardata.bmwgroup.com`, header **`x-version: v1`** (without it:
  403) + Bearer. **[op]**
  - `GET /customers/vehicles/mappings` → entries with `vin`, `mappingType` (`PRIMARY`). **[evcc]**
  - `GET|POST /customers/containers`, `DELETE /customers/containers/{id}` — a **container** is
    a named set of telematic keys (`technicalDescriptors`), `state` ACTIVE | DELETED. **[bmw]**
  - `GET /customers/vehicles/{vin}/telematicData?containerId={id}` →
    `{"telematicData": {"<key>": {"value", "unit", "timestamp"}}}`. **[op]**
- **Quota: ~50 requests / 24 h**, per Client ID **[op; window and headers [?]]** — shared with
  anything else the owner runs on the same Client ID. **Read-only**: no command endpoints. **[op]**
- Telematic keys used (from the operator's container + evcc): see `bmw.CONTAINER_KEYS`.
  Availability varies by model; a key the car doesn't report is simply absent.

## Decisions

1. **Sign-in = brand `bmw` on the existing car form, then a confirm step.** The form gets a
   brand picker (Škoda / BMW & Mini); `site.js` shows the picked brand's fields (no-JS
   fallback: all fields shown, server validates per brand). BMW fields: **Client ID**, **VIN**,
   email. `start_login` requests a device code and returns `SecondFactorNeeded`; the confirm
   page shows the **user code** and a **link to BMW's page** where it's typed in and approved, plus an "I've approved it"
   button. `resume_second_factor` polls the token endpoint **once** per click:
   pending → `SecondFactorError` (re-prompt, same state); expired / denied → `LoginError`.
   - **Core change (small, generic):** an adapter may define
     `second_factor_fields(state) -> dict[str, str]` (probed with `getattr`, like
     `readiness_path`); `oauth.py` merges it into the confirm page's `_fill` mapping on the
     first render and on re-prompt. Values are HTML-escaped by `_fill` as usual.
   - Pending state lives in `AuthState` (TTL 300 s) — enough for a device code.
2. **Sign-in proves the VIN and prepares a container (≤ 3 upstream calls).** After the token:
   `GET mappings` — the VIN must be a `PRIMARY` mapping of this BMW account, else a form error.
   Then `GET containers`: reuse an ACTIVE one named `missingmcp`, else `POST` a new one with
   `CONTAINER_KEYS`. The VIN check makes `credential_subject` = `bmw:<VIN>` an identity the
   upstream proved (same guard as Škoda).
3. **Blob** (`v: 1`): `brand`, `client_id`, `vin`, `access_token`, `refresh_token`,
   `access_expires_at` (epoch s), `container_id`, `operations: []`. No `expires_at`: the
   credential has no fixed expiry. The id token and `gcid` are not kept (no streaming in v1).
   `verify` stays offline: shape check per brand.
4. **Token refresh = the WHOOP rule.** `BmwClient.ensure_fresh`: refresh when < 120 s left,
   serialized per account under an `asyncio.Lock`, re-read the stored blob inside the lock,
   **persist the rotated blob before using it** — compare-and-set on (`client_id`, `vin`) so a
   concurrent reconnect isn't overwritten. `invalid_grant` (or 401 from the API after a
   refresh) → `SessionExpired` → re-auth 401. The account is **not** deleted (no WHOOP-style
   terms obligation). Event names: `car-refresh-ok`, `car-refresh-failed`.
5. **Quota handling.** One telematic read serves both status and location: the raw
   `telematicData` is cached per account+VIN for **15 min** (`bmw` status cache; Škoda keeps
   60 s), and `get_vehicle_location` reuses a fresh cache. The gateway counts its own CarData
   calls per VIN over a rolling 24 h (process-local, resets on deploy) and reports
   `budget: {"used_last_24h": n, "limit": 50}` — honest that other apps on the same Client ID
   also spend it. Upstream 429 → tool error "BMW's daily limit … try again tomorrow".
6. **Status shape = the Škoda shape**, filled where CarData has the value: `battery_percent`,
   `range_km`, `odometer_km`, `charging {state, plug, minutes_to_full, limit_percent}`,
   `doors`, `trunk`, `bonnet`, `climate {state}`, each block with `captured_at` from the key's
   timestamp. Plus `data_age_note`: CarData values are what the car last reported. Location
   only via `get_vehicle_location` (`lat`, `lon`, `captured_at`; no address).
7. **Brand-neutral MCP server.** `mcp.py` dispatches per brand through a small client
   interface (`read_status`, `read_location`, `action`) instead of calling `skoda.*` directly;
   error texts and the expiry notice become per-brand. Škoda behaviour stays byte-identical
   (its tests are the regression gate).
8. **Site:** BMW & Mini card flips from *Soon* to *Beta* with setup steps (MyBMW portal →
   BMW CarData → create Client ID, tick CarData API); the authorize page explains the confirm
   step. Hyundai & Kia stay *Soon*.

## Testing

- A fake CarData upstream in `tests/conftest.py` (`fake_bmw`): device code, token
  (pending → ok, refresh with rotation, `invalid_grant`), mappings, containers, telematicData
  with sentinel location values, 429.
- Unit/integration: the two-step sign-in through `/car/oauth/authorize` (pending re-prompt,
  wrong VIN, expired code), container reuse vs create, refresh-before-use persistence +
  concurrent refresh under the lock, status/location sharing one read, quota counter, 429 and
  401 mapping, privacy (status never carries location).
- **Release gate:** the operator's own i3 — note whether a second device-flow sign-in on the
  same Client ID leaves the Grok bot's tokens working, and read the real quota headers.

## Out of scope

- MQTT streaming (needs a long-lived connection per account).
- Charging history, basic data, images.
- Several VINs per account.
