# 05 — v1 tool set and rate-limit handling

Type: grilling
Status: resolved
Blocked by: 01, 02

## Question

Which tools ship in v1 (read state, SoC/charging, AC / aux heat / ventilation start-stop),
with what names and parameters; how actions behave (confirmation, temperature/duration
params, async completion, idempotency); and how the ~20 req/h upstream budget maps to
gateway behaviour (caching reads, per-VIN throttle, friendly `Retry-After` errors)?

Constraints from 01: actions return 202 with no op id — confirmation costs a re-read from
the same 20/h budget (avoid auto-polling); two distinct 429s (quota vs
`vehicle-not-accepting-requests`), told apart only by `type`; 422 `operation-not-supported` /
`operation-disabled`; 504 = car didn't answer; aux heating requires the S-PIN.

Already decided elsewhere: brand-neutral tool names + a "what can this vehicle do" tool
(03); **no aux heating in v1** because the S-PIN is never stored (04); tool results carry the
key-expiry notice from 14 days before expiry (04). From 02: a sleeping car needs ~2 min to
reflect an action, an awake one ~4 s; freshness only via `carCapturedTimestamp`;
`operations[]` per car; a full read contains GPS, address, plate and VIN; the 20/h budget is
per VIN and shared with the owner's other keys.

## Answer

Decided with Vaclav, 2026-10-28.

**v1 tools** (brand-neutral names; cost = calls against the 20/h per-VIN budget):

| Tool | Upstream call | Cost |
|---|---|---|
| `get_vehicle_capabilities` | none: brand, supported actions, key expiry and last known budget, from the blob and memory | 0 |
| `get_vehicle_status` | `GET ?include=status,charging,airConditioning,activeVentilation,odometer`: locks/doors/windows, battery %, range, charging, climate, ventilation, odometer, each with `captured_at` | 1 |
| `get_vehicle_location` | `GET ?include=parkingPosition`: called only when the user asks where the car is | 1 |
| `start_climate(target_temperature_c?, allow_battery?)` / `stop_climate` | AC start/stop (`allow_battery` → `airConditioningWithoutExternalPower`) | 1 |
| `start_ventilation` / `stop_ventilation` | active ventilation (no S-PIN) | 1 |
| `start_charging` / `stop_charging` | charging start/stop | 1 |
| `set_charge_limit(percent)` | 50–100 in steps of 10 | 1 |

- **Per-car tool list:** `verify` (ticket 04) requests `include=info,operations` (still one call)
  and stores `operations[]` in the blob. `tools/list` shows **only the tools this car supports**.
  Every `get_vehicle_status` also requests `operations` and refreshes the stored list.
  `get_vehicle_capabilities` states the same in words.
- **Privacy:** `get_vehicle_status` never returns GPS, address, licence plate, car name or the
  full VIN (last 6 chars only). Location lives only in `get_vehicle_location`.
- **Actions:** return immediately, e.g. "Command accepted; a sleeping car can take ~2 min to
  react; check with `get_vehicle_status` later". No server-side waiting or polling. No
  confirmation parameter: the MCP client asks before write tools, and reads carry
  `readOnlyHint`. Repeated starts are passed through as-is.
- **Budget:** every result carries `budget: {remaining, resets_in_min}` from `RateLimit-*`. The
  last value is kept in memory per VIN. Status reads are **cached for 60 s** per account; a cached
  answer says `cached: true` and costs nothing. No gateway-side throttle below the upstream's 20.
- **Errors are tool errors, not re-auth:** quota 429 → "Škoda's 20 requests/hour limit is used
  up; resets in N min". 429 `vehicle-not-accepting-requests` → "the car isn't accepting commands
  now (low battery / operation in progress)". 422 → "this car doesn't support this / has it
  disabled". 504 → "the car didn't respond; try again". Only 401 maps to re-auth (ticket 04).
- **Output shape:** JSON with brand-neutral English keys (`battery_percent`, `range_km`,
  `climate: {state, target_c}`, …); metric units, °C, UTC ISO timestamps. The key-expiry notice
  (ticket 04) rides as a `notice` field.
- **Not in v1 (fog):** charge mode, charging profiles and timers; auxiliary heating (S-PIN,
  ticket 04).
