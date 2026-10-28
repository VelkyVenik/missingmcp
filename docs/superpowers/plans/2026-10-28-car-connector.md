# Car Connector Implementation Plan

> **For agentic workers:** Work task by task, test-first (`/tdd`). Steps use checkbox
> (`- [ ]`) syntax for tracking. Every task ends with the full suite green and one commit.

**Goal:** a generic `car` adapter at `/car/mcp`. A person picks a brand (only Škoda today),
types their email, MyŠkoda API key and VIN on our form. Claude then gets brand-neutral car tools
(status, location, climate, ventilation, charging), served in-process.

**Spec:** `docs/superpowers/specs/2026-10-28-car-connector-design.md`. Decisions and their
reasons: `.scratch/car-connector/` (map + tickets). Live upstream observations:
`.scratch/car-connector/assets/02-live-key-observations.md`. Upstream contract:
`.scratch/car-connector/assets/skoda-public-api-1.1.0.yaml`.

**Architecture:** no new seams. The `car` adapter reuses the **form-login** shape (like garmin, no
second factor) and the **local forward** strategy (like whoop). New code is confined to
`src/missingmcp/adapters/car/` (adapter, Škoda client, MCP layer), two templates, one generator
script, a test fixture and tests. The core (`oauth.py`, `proxy.py`, `app.py`) needs no changes
beyond registration in `adapters/__init__.py` and one config field.

**Tech stack:** Python 3.12, Starlette, httpx, SQLite. **No new dependencies.**

## Global constraints

- Tests: `uv run --extra dev pytest -q` from the worktree root
  (`/Users/vaclav.slajs/dev/webs/missing-mcp-car-connector`). `--extra dev` is REQUIRED. Baseline:
  **538 passed**. The full suite must be green at the end of every task.
- Never touch `adapters/garmin/` or `adapters/whoop/` behavior. Never import `garmin_mcp`.
- **No real Škoda in the suite.** Every upstream interaction goes to the `fake_skoda` fixture
  (Task 2) via `CAR_SKODA_API_BASE`.
- **Secrets:** the API key and VIN live ONLY in the encrypted blob. They never appear in logs,
  telemetry, exceptions' `str()` that reach logs, or files. Tests assert the key and VIN are
  absent from captured log output (Task 6).
- **Privacy:** `get_vehicle_status` output never contains GPS, address, licence plate, car name or
  the full VIN (Task 4 asserts this). Location appears only in `get_vehicle_location`.
- **Log schema is stable.** Login failures use the EXISTING generic events
  (`login-start-failed` with `reason`, `login-verify-failed`). The `reason` values for car are
  exactly: `invalid_form`, `invalid_key`, `vin_not_covered`, `rate_limited`, `upstream_error`.
  Exactly one new event: `car-upstream-error` (fields: `account` = 8-char hash prefix as elsewhere,
  `tool`, `status`, `problem` = the problem `type` tail). The existing re-auth path logs the
  expired-key case. *(This refines the spec's event list, which proposed `car-login-*` and
  `car-key-expired`; the generic events already carry that information. Task 8 amends the spec.)*
- Verify-then-persist stays intact. `start_login` makes the single upstream call; `verify` is
  offline (spec → Login).
- `display_name = "Car"` (brand-neutral: the connector is not Škoda-specific; it appears in
  the re-auth message "Settings → Connectors → Car" and landing titles).
- Commit after every task with the message given. Append:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Upstream facts to copy verbatim

- Base: `https://public.api.connect.skoda-auto.cz`. Auth header: `X-API-Key: <key>`.
- Read: `GET /api/v1/vehicles/{vin}?include=<comma list>`. Includes: `info, status, fuelStatus,
  odometer, parkingPosition, airConditioning, auxiliaryHeating, activeVentilation, charging,
  chargingProfiles, operations`. Body: `{"vehicle": {...}, "errors": [{"type", "description"}]}`.
  `*_UNSUPPORTED` errors appear only for explicitly requested includes.
- Actions (all `202`, no body):
  - `POST …/air-conditioning/start`, optional body
    `{"targetTemperature": {"value": <float>, "unit": "CELSIUS"}, "airConditioningWithoutExternalPower": <bool>}`;
  - `POST …/air-conditioning/stop`;
  - `POST …/active-ventilation/start|stop` (no body);
  - `POST …/charging/start|stop`;
  - `PUT …/charging/limit` with body `{"targetStateOfChargeInPercent": <int>}`.
- `operations[]` items: `{"name": "<operationId>"}`, e.g. `startAirConditioning`,
  `stopAirConditioning`, `startActiveVentilation`, `stopActiveVentilation`, `startCharging`,
  `stopCharging`, `setChargingLimit`.
- Headers:
  - on every 2xx: `X-API-Key-Expires-At` (RFC 3339) and `RateLimit-Limit`, `RateLimit-Remaining`,
    `RateLimit-Reset` (seconds);
  - on 429: also `Retry-After` (seconds).
- Errors: `application/problem+json` with `type`. Map on the `type` tail after `/problems/`:
  - 401 `api-key-expired` or `about:blank` → expired/invalid key;
  - 403 `api-key-not-authorized` → VIN not covered;
  - 422 `operation-not-supported` / `operation-disabled`;
  - 429 `vehicle-not-accepting-requests` vs any other 429 = quota;
  - 504 → car didn't respond.
- `info` gives only `name` (user-defined, or the model name when unnamed), `licensePlate` and
  `renderUrl`. There is no model field, so we never request `info`.
- AC `targetTemperature.value` has no range in the contract. Validate 15–30 °C locally as a sanity
  bound and let the upstream reject the rest.
- Live shapes (redacted) for the fake: `.scratch/car-connector/assets/02-live-key-observations.md`
  → "Full read, redacted shape".

---

### Task 1: Config: Škoda API base

**Files:** modify `src/missingmcp/config.py`; test `tests/test_config.py`.

- [ ] Failing tests: `load_config(BASE).car_skoda_api_base == "https://public.api.connect.skoda-auto.cz"`;
  `CAR_SKODA_API_BASE="http://127.0.0.1:9999/"` → trailing slash stripped. (Pattern: the
  `whoop_api_base` tests.)
- [ ] Add `car_skoda_api_base: str` to `Config` and `load_config` (`.rstrip("/")`), with a
  one-line comment like `whoop_api_base`'s.
- [ ] Suite green. Commit: `feat(car): config — Škoda API base override`

### Task 2: Fake Škoda upstream fixture

**Files:** modify `tests/conftest.py` (new `FakeSkodaUpstream(_FakeHttpServer)` + `fake_skoda`
fixture); test `tests/test_car_skoda.py` (fixture sanity only here).

Knobs (all mutable attributes, recorded `calls` like the other fakes):
- `keys: dict[str, set[str]]` maps an API key to the VINs it covers; default
  `{"key-1": {"TMBTEST0000000001"}}`.
- `vehicle: dict` is the full `vehicle` object, seeded from the redacted live shape (EV: AC +
  charging operations only, `errors` with `AUXILIARY_HEATING_UNSUPPORTED` and
  `ACTIVE_VENTILATION_UNSUPPORTED`). Its `licensePlate`, `name`, `formattedAddress` and
  `gpsCoordinates` hold recognizable sentinels (`PLATE-SENTINEL`, `NAME-SENTINEL`,
  `ADDR-SENTINEL`, lat `49.123456`) so privacy tests can grep for them.
- `expires_at = "2027-04-26T09:11:23.249Z"`, `remaining = 20`. Each counted call decrements
  `remaining`; 401/403/404 don't. `reset = 3600`.
- `force: tuple[int, str] | None` makes the next call(s) answer `(status, problem-type-tail)`,
  e.g. `(429, "rate-limit-exceeded")`, `(429, "vehicle-not-accepting-requests")`,
  `(422, "operation-not-supported")`, `(504, "gateway-timeout")`, `(401, "api-key-expired")`.
  A quota 429 carries `Retry-After: 1200`.
- `delay_s: float = 0` sleeps before answering (timeout tests).

Behavior:
- An unknown key → 401 `about:blank`.
- A VIN the key doesn't cover → 403 `…/problems/api-key-not-authorized`.
- `GET /api/v1/vehicles` with no VIN → 404.
- `GET /api/v1/vehicles/{vin}?include=a,b` returns only the requested sections, plus `errors`
  for unsupported requested sections. `info` maps to top-level `name`, `licensePlate` and
  `renderUrl` (yaml contract; the API exposes **no model** field).
- Actions answer 202 with an empty body and mutate state realistically: AC start sets
  `airConditioning.state="HEATING"` and stores `targetTemperature`; stop sets `"OFF"`;
  charge-limit sets `charging.settings.targetStateOfChargeInPercent`.
- `do_PUT` exists (charge limit).
- Every 2xx carries the expiry and `RateLimit-*` headers.

- [ ] Sanity tests: unknown key → 401; foreign VIN → 403 not counted; include filtering; an
  action changes the next read; `remaining` decrements only on counted calls.
- [ ] Commit: `test(car): fake Škoda Public API upstream`

### Task 3: Škoda brand client (`adapters/car/skoda.py`)

**Files:** create `src/missingmcp/adapters/car/__init__.py` (empty for now) and
`src/missingmcp/adapters/car/skoda.py`; test `tests/test_car_skoda.py`.

Interface (brand-client contract; a future brand implements the same names):

```python
class CarUpstreamError(Exception):
    """kind: "invalid_key" | "vin_not_covered" | "rate_limited" | "car_busy" |
    "unsupported" | "car_timeout" | "upstream_error"; retry_after_s: int | None;
    status: int; problem: str (type tail). str(e) never contains the key or VIN."""

@dataclass
class Budget:
    remaining: int | None; reset_s: int | None; at: float  # time.monotonic() of observation

@dataclass
class Reply:
    data: dict | None          # parsed JSON for reads, None for 202
    budget: Budget | None
    expires_at: str | None

class SkodaClient:
    def __init__(self, base_url: str, timeout_s: float = 15.0): ...
    def probe_sync(self, api_key: str, vin: str) -> Reply:
        """Sign-in check: GET ?include=operations (sync httpx; runs inside _bounded)."""
    async def read(self, api_key: str, vin: str, includes: list[str]) -> Reply: ...
    async def action(self, api_key: str, vin: str, op: str, body: dict | None) -> Reply:
        """op is an operationId: startAirConditioning, stopAirConditioning, startActiveVentilation,
        stopActiveVentilation, startCharging, stopCharging, setChargingLimit → method + path."""
```

- [ ] Failing tests against `fake_skoda`:
  - `probe_sync` happy path returns `data.vehicle.operations` and `expires_at`, and `budget`
    parsed from the headers;
  - read include filtering;
  - each action op hits the right method and path with the right body;
  - error mapping for 401 (both types), 403, 422 (both), 429 quota (`retry_after_s=1200`), 429
    car-busy, 504, 500 and a connect error, each to the right `kind`;
  - the key and VIN don't appear in `str(err)`.
- [ ] Implement with `httpx.Client` / `httpx.AsyncClient` (a fresh client per call is fine;
  volume is ≤20/h per car). VIN in the URL path is uppercased and validated (17 chars,
  `[A-HJ-NPR-Z0-9]`) before any request.
- [ ] Commit: `feat(car): Škoda Public API client with problem-type error mapping`

### Task 4: Brand-neutral normalization

**Files:** add `normalize_status(vehicle: dict, errors: list) -> dict` and
`normalize_location(vehicle: dict) -> dict` to `skoda.py` (brand-specific mapping into the
neutral shape); test `tests/test_car_skoda.py`.

Neutral status shape (spec → Tools). Omit a key when the section is absent or unsupported:

```json
{"vehicle": {"brand": "skoda", "vin_tail": "000001"},
 "locks": {"locked": true, "doors_locked": true, "captured_at": "…"},
 "doors": "closed", "windows": "closed", "trunk": "closed", "bonnet": "closed", "lights": "off",
 "battery_percent": 75, "range_km": 296,
 "charging": {"state": "not_connected", "plug": "disconnected", "power_kw": 0.0,
              "limit_percent": 80, "minutes_to_full": 0, "captured_at": "…"},
 "climate": {"state": "off", "target_c": 22.0, "window_heating": {"front": "off", "rear": "off"},
             "captured_at": "…"},
 "ventilation": {"state": "off", "captured_at": "…"},
 "odometer_km": 8279,
 "unsupported": ["auxiliary_heating", "ventilation"]}
```

- Enum values are lower-cased Škoda values (`CONNECT_CABLE` → `"connect_cable"`); keep it
  mechanical, no invented semantics. `range_km` = `remainingCruisingRangeInMeters / 1000`,
  rounded. `captured_at` = that section's `carCapturedTimestamp`.
- [ ] Failing tests:
  - mapping from the fake's full read;
  - **privacy:** `json.dumps(normalize_status(...))` contains none of `PLATE-SENTINEL`,
    `NAME-SENTINEL`, `ADDR-SENTINEL`, `49.123456`, or the full VIN;
  - `normalize_location` returns `{"parked", "address", "lat", "lon", "captured_at"}`;
  - unsupported sections are listed, not crashing.
- [ ] Commit: `feat(car): brand-neutral status and location shapes`

### Task 5: Car MCP server (`adapters/car/mcp.py`)

**Files:** create `src/missingmcp/adapters/car/mcp.py`; test `tests/test_car_mcp.py`.

Copy whoop's JSON-RPC skeleton (`PROTOCOL_VERSIONS`, `_result`, `_rpc_error`, `_tool_error`,
`handle` dispatch). Copying is acceptable. If a tiny shared helper falls out cleanly, extracting
it to `adapters/_jsonrpc.py` is allowed only with whoop's tests untouched and green.
`SERVER_INFO = {"name": "missingmcp-car", "version": "1.0.0"}`.

Tool table: `(name, description, input_schema, operation | None, read_only, fn)`. `operation`
gates visibility (None = always shown).

| name | operation | read_only | upstream |
|---|---|---|---|
| `get_vehicle_capabilities` | None | ✓ | none |
| `get_vehicle_status` | None | ✓ | read `status,charging,airConditioning,activeVentilation,odometer,operations` |
| `get_vehicle_location` | None | ✓ | read `parkingPosition` |
| `start_climate` | `startAirConditioning` | ✗ | args `target_temperature_c` (number 15–30, optional), `allow_battery` (bool, optional) |
| `stop_climate` | `stopAirConditioning` | ✗ | |
| `start_ventilation` / `stop_ventilation` | `startActiveVentilation` / `stopActiveVentilation` | ✗ | |
| `start_charging` / `stop_charging` | `startCharging` / `stopCharging` | ✗ | |
| `set_charge_limit` | `setChargingLimit` | ✗ | `percent` integer, enum 50,60,70,80,90,100 |

Descriptions must make the model economical, e.g.:
- `get_vehicle_location`: "Only call when the user asks where the car is."
- action tools: "A sleeping car can take ~2 minutes to react; don't poll — check
  get_vehicle_status later if the user asks."
- `get_vehicle_status`: "Counts against the car's 20 requests/hour; cached for 60 s."

`CarLocalForward(config)`:
- `handle(conn, account_key, blob, body)` follows the whoop dispatch. `tools/list` returns the tools
  whose `operation` is None or in `blob["operations"]`, with
  `annotations: {"readOnlyHint": read_only}`. `tools/call` on a hidden tool (operation not in
  the blob) is a tool error: "This car doesn't support <tool>."
- Brand dispatch: `{"skoda": SkodaClient(config.car_skoda_api_base)}[blob["brand"]]`. An unknown
  brand → tool error (it can't happen through the form).
- **Budget:** `self._budget: dict[str, Budget]` keyed by VIN, updated from every `Reply`. The
  result payload is `budget: {"remaining": n, "resets_in_min": ceil((reset_s - elapsed)/60)}`,
  omitted when unknown.
- **Cache:** `self._status_cache: dict[str, tuple[float, dict]]` keyed by `account_key`, 60 s TTL.
  A hit returns the cached payload plus `"cached": true` and makes no upstream call. Any action
  call clears that account's entry.
- **Operations refresh:** when a status read's `operations` differ from the blob's, persist the
  updated blob (`store.upsert_account(conn, "car", account_key, json.dumps(new), config.gateway_secret)`)
  before returning.
- **Expiry notice:** when `now >= expires_at - 14 days`, add
  `"notice": "Your Škoda API key expires on <YYYY-MM-DD>. Create a new one in the MyŠkoda app (Profil → Chytrá domácnost) and reconnect."`.
  Use an injectable `now` callable for tests.
- **Action results:**
  `{"accepted": true, "message": "Command sent. A sleeping car can take ~2 minutes to react — check with get_vehicle_status later.", "budget": …}`.
- **Errors:** `CarUpstreamError` kind → tool-error text per the spec's table (include the reset
  minutes for quota). `invalid_key` (401) → `raise SessionExpired(...)` so the proxy answers
  re-auth 401. Every non-401 upstream error logs `car-upstream-error`.
- **Argument validation** (bad temperature, `percent` not in the enum, wrong types) → tool error
  before any upstream call (whoop's `_call` pattern, incl. `OverflowError`).
- [ ] Failing tests (drive `CarLocalForward.handle` directly with an in-memory store conn and
  hand-built blobs against `fake_skoda`):
  - initialize/ping/notifications;
  - `tools/list` filtered (EV blob → no ventilation tools; an ICE-like blob with ventilation ops
    → listed) with the correct `readOnlyHint` per tool;
  - each tool's upstream call, and the location tool is the only one returning sentinels;
  - cache hit makes no second upstream call, and an action clears the cache;
  - `budget` shown;
  - operations refresh persists the new blob;
  - expiry notice at 13 days but not at 15;
  - every error mapping;
  - 401 → `SessionExpired`;
  - invalid args make no upstream call;
  - a hidden tool call → tool error.
- [ ] Commit: `feat(car): in-process MCP server with per-car tools, cache and budget`

### Task 6: `CarAdapter`, registration, authorize form, end-to-end

**Files:**
- modify `src/missingmcp/adapters/car/__init__.py` (`CarAdapter`) and
  `src/missingmcp/adapters/__init__.py` (always register `"car"`);
- create `src/missingmcp/templates/car_authorize.html` and a minimal
  `src/missingmcp/templates/car.html` stub (Task 7 fills it in);
- test `tests/test_car_e2e.py`.

`CarAdapter`:
- `name = "car"`, `display_name = "Car"`, `authorize_template = "car_authorize.html"`,
  `second_factor_template = ""`, `landing_template = "car.html"`,
  `forward = CarLocalForward(config)`.
- `login_hint(form)` returns `form["car_email"]`.
- `start_login(form)`:
  1. Validate first (no network). Brand must be `skoda`; email must contain `@`; the key must be
     non-empty; the VIN is uppercased and stripped and must be 17 valid chars. Otherwise
     `LoginError(<friendly>, reason="invalid_form")`.
  2. `SkodaClient.probe_sync(key, vin)`.
  3. On success, return `LoginOk(account_key=normalize_account_key(email), blob=json.dumps({"v": 1, "brand": "skoda", "api_key": key, "vin": vin, "expires_at": reply.expires_at, "operations": [...]}))`.
  4. Map errors to `LoginError(<spec copy>, reason=...)`: `invalid_key`, `vin_not_covered`,
     `rate_limited` (with minutes from `retry_after_s`), `upstream_error`.

  Never keep the key on `self`.
- `verify(blob)`: offline. Parse the JSON, require `brand`, `api_key`, `vin`, `expires_at` (not
  in the past) and an `operations` list. Return `"Škoda"` (the API has no model; the user-defined car name stays out of logs). Otherwise
  `LoginError("…please try again.")`.
- `resume_second_factor` raises `LoginError` (unreachable).

`car_authorize.html` (fragment; copy `authorize.html`'s structure and placeholders: `{ERROR}`,
`{AUTHORIZE_ACTION}`, `{OAUTH_FIELDS}`, `{OPERATOR}…`):
- `<select name="car_brand">` with a single `<option value="skoda">Škoda</option>`;
- `car_email` (type=email, plus a hint: "used only for important news about this connector");
- `car_api_key` (type=password, `autocomplete="off"`);
- `car_vin` (`maxlength=17`, `autocapitalize="characters"`).

Setup steps go above the fields: "MyŠkoda app → Profil → Chytrá domácnost → Vytvořit klíč →
Kopírovat klíč; the VIN is on the same screen". Add a ToS line: personal use; the key is stored
encrypted; revoke it in the app any time. No screenshot in this task (Task 7 decides).

- [ ] Failing e2e tests through `build_app` (whoop e2e pattern, with
  `CAR_SKODA_API_BASE` → `fake_skoda`):
  - discovery documents for `car`;
  - DCR → `GET /car/oauth/authorize` renders the form → POST the form → 302 with code → `/token`
    → Bearer;
  - `tools/list` filtered;
  - `get_vehicle_status` works;
  - a form error per reason re-renders with the right message (and `invalid_form` makes no
    upstream call);
  - **the key and VIN never appear in captured log output** (use the existing log-capture
    helper; grep the whole stream);
  - an upstream `force=(401,"api-key-expired")` on a tool call → HTTP 401 with the RFC 9728
    challenge;
  - GET/DELETE `/car/mcp` → 405;
  - `/car` landing renders.
- [ ] Commit: `feat(car): car adapter — form login, registration, end-to-end`

### Task 7: Landing page `/car` + generated tool list

**Files:** fill `src/missingmcp/templates/car.html`; create `scripts/gen_car_tools.py`
(`gen_whoop_tools.py` pattern; group into "Read" and "Actions"; note that actions show only
when the car supports them).

Follow the `garmin.html` header-comment section order (spec → Landing page):
1. hero with a **Beta** pill and the server URL `{PUBLIC_URL}/car/mcp`;
2. What is this?;
3. **Supported brands**: Škoda only, status "available (beta)", what works, setup steps, a link
   to `https://public.api.connect.skoda-auto.cz/docs`;
4. What Claude can do;
5. How to connect;
6. Tips & tricks;
7. **Limits**: 20 requests/hour per car shared with your other keys; a sleeping car reacts in
   ~2 min; the key lasts ~6 months;
8. **Terms & risks**: personal use per Škoda's terms; unofficial beta; key stored encrypted;
   revoke in the app;
9. Under the hood;
10. All tools (generated);
11. CTA.

Include `{USAGE_METER_CAR}` where garmin has its meter. **No homepage change.**

- [ ] Run `python scripts/gen_car_tools.py`. Add a test in the existing landing-page test style:
  `/car` renders and contains every tool name from `TOOLS`.
- [ ] Commit: `feat(car): /car landing page with brand list and generated tool list`

### Task 8: Docs

**Files:** `README.md`, `CLAUDE.md`, the spec.

- README: add the connector, `CAR_SKODA_API_BASE` (tests/staging only), the user setup steps,
  and the beta status.
- CLAUDE.md:
  - Modules list: `adapters/car/` as the form-login adapter on the local strategy (Škoda client,
    in-process MCP).
  - Cross-cutting invariants, one bullet: car `account_key` is a typed, unverified email; the
    key and VIN live only in the blob; status output never carries location; `start_login` makes
    the single upstream call and `verify` is offline (a 20/h-per-VIN budget).
  - Commands: `python scripts/gen_car_tools.py`.
- Spec: amend the Security section's events to match Global constraints (generic login events
  with car reasons, plus `car-upstream-error`).
- [ ] Commit: `docs(car): README, CLAUDE.md, spec event list`

## Post-plan release gate (operator, manual)

On Vaclav's real Škoda, sparing the 20/h (the Grok bot shares it), from a deploy of this branch:
1. Connect via Claude (discovery → DCR → form → token).
2. `get_vehicle_capabilities`.
3. `get_vehicle_status`.
4. `start_climate` → wait ~2 min → `get_vehicle_status` → `stop_climate`.
5. `get_vehicle_location` once.

That is ≤7 counted calls. Compare against `.scratch/car-connector/assets/02-live-key-observations.md`.
Then PR to `main` (review via Codex).

## Self-review notes

- Spec coverage: login (T6), blob (T6), local forward + MCP (T5), tools (T5), privacy (T4/T5),
  rate limit/cache/budget (T5), expiry notice (T5), re-auth (T5/T6), config (T1), landing (T7),
  security/logs (global + T6), testing (T2–T7), docs (T8).
- Deliberate refinement vs spec: log events (see Global constraints); spec amended in T8.
- Contract check at planning time: `info` has no model (dropped from blob and output), and AC
  temperature has no range (local 15–30 °C sanity bound). No user decision is pending.
