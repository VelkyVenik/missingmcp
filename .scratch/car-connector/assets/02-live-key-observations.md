# 02 — Live key observations (Vaclav's Škoda, 2026-10-28)

Car: battery-electric Škoda (one car on the account), MyŠkoda Public API 1.1.0, one key
("Test MissingMCP"). Identifiers redacted: VIN → `<VIN>`, plate, GPS, address, car name, render
URL. Key and VIN live only in `~/.config/missingmcp/` (0600), never in the repo.

## Key lifecycle (MyŠkoda app)

- Keys are created **only in the mobile app**: Profil → Chytrá domácnost → Vytvořit klíč →
  name → Kopírovat klíč. No web flow.
- **Lifetime is fixed at ~6 months** (created 2026-10-28, `X-API-Key-Expires-At:
  2027-04-26T09:11:23.249Z` = 180 days). It can't be chosen in the app.
- **Max 5 keys per account** (app shows "Zbývající klíče 3/5" with two keys present).
- The vehicle is picked when the key is created. The same app screen shows **"VIN vozidla" with
  a copy button**, next to the key list, so a connect form can say "copy the VIN from the same
  screen".
- The app shows no terms or consent text at key creation.
- Other keys on the same car (here "Grok Bot") **share the per-VIN budget**.

## Calls made (7 counted against the 20/h budget)

| # | UTC | Call | Status | `RateLimit-Remaining` / `Reset` | Notes |
|---|---|---|---|---|---|
| 1 | 09:14:53 | `GET /api/v1/vehicles` (no VIN) | 404 `about:blank` "No static resource api/v1/vehicles." | — (no headers) | **No list endpoint.** Not counted. |
| 2 | ~09:15 | `GET /vehicles/<VIN>?include=<all 11>` | 200, 3.2 kB | 19 / 3600 | `X-API-Key-Expires-At` present |
| 3 | ~09:16 | `GET /vehicles/<other VIN>?include=info` | 403 `problems/api-key-not-authorized` | — | **Not counted** (next call showed 18) |
| 4 | ~09:16 | `GET /vehicles/<VIN>?include=airConditioning` | 200 | 18 / 3581 | |
| 5 | 09:21:12 | `POST …/air-conditioning/start` `{targetTemperature 20 °C, airConditioningWithoutExternalPower:true}` | 202, returned in <1 s | 17 / 3442 | |
| 6 | 09:21:48 | `GET …?include=airConditioning` | 200 | 16 / 3406 | still `OFF`, `carCapturedTimestamp` unchanged (08:32:56) |
| 7 | 09:23:56 | `GET …?include=airConditioning` | 200 | 15 / 3278 | `HEATING`, captured 09:23:25, `estimatedReachOfTargetTemperatureAt` 09:52:57, window heating front/rear `ON` |
| 8 | 09:24:04 | `POST …/air-conditioning/stop` | 202 | 14 / 3270 | app confirmed off shortly after |
| 9 | 09:25:38 | `GET …?include=airConditioning` | 200 | 13 / 3177 | `OFF`, captured **09:24:08** (4 s after stop), window heating `OFF`, target now 20 °C (persisted) |

## Findings

- **Rate limit: fixed 1-hour window per VIN**, starting at the first counted request. `Reset`
  counts down in real time (3600 → 3581 → … → 3270). Per VIN is confirmed by the docs landing
  page; Vaclav: take the worst case, don't test with a second key.
- **Not counted:** 404 (unknown path) and 403 (wrong VIN). Counted: 200 and 202.
- **Action latency:** `202` comes back immediately, but the car reported the new state only after
  **~2 min 13 s** (09:21:12 → 09:23:25). A read 36 s after the action showed the old state with
  an old `carCapturedTimestamp`. **`carCapturedTimestamp` is the only signal that a read is
  fresh.** The app showed "Probíhá…" (in progress) meanwhile. The **stop** took effect in **~4 s**
  (09:24:04 → captured 09:24:08). So the ~2 min was the car waking up and the climate starting;
  an already-awake car responds within seconds. A start also **persists the target temperature**
  (22 → 20 °C stayed after stop). Starting AC also switched window heating front and rear on.
- **AC auto-stops after 30 min** (app message; matches `estimatedReachOfTargetTemperatureAt` ≈
  start + 30 min). The API has no duration parameter for AC.
- The car was parked and locked (likely asleep) and still woke up for the AC command. No 504
  was seen.
- **`operations[]` is per-car**: this EV lists only `startCharging, stopCharging,
  setChargingLimit, setChargeMode, updateChargingProfile, startAirConditioning,
  stopAirConditioning`. `errors[]` returns `FUEL_STATUS_UNSUPPORTED`,
  `AUXILIARY_HEATING_UNSUPPORTED` and `ACTIVE_VENTILATION_UNSUPPORTED` for those includes. The
  tool surface should follow `operations[]`.
- **Privacy-sensitive fields in a full read:** `parkingPosition.formattedAddress`,
  `gpsCoordinates` (lat/lon), `licensePlate`, `name`, `renderUrl`, `vin`. Decide in 05 what
  reaches the model.
- A full read is ~3.2 kB. `chargingProfiles` takes most of it (4 preferred-time slots and 3
  timers per profile).

## Full read, redacted shape (call 2)

```json
{
  "vehicle": {
    "airConditioning": {"state": "OFF", "airConditioningAtUnlock": true,
      "carCapturedTimestamp": "…Z", "targetTemperature": {"value": 22.0, "unit": "CELSIUS"},
      "windowHeating": {"enabled": true, "front": "OFF", "rear": "OFF"}},
    "charging": {"isVehicleInSavedLocation": true, "carCapturedTimestamp": "…Z",
      "settings": {"autoUnlockPlugWhenCharged": "OFF", "availableChargeModes": ["MANUAL"],
        "batteryCareModeTargetValueInPercent": 80, "chargingCareMode": "ACTIVATED",
        "maxChargeCurrentAc": "MAXIMUM", "preferredChargeMode": "MANUAL",
        "targetStateOfChargeInPercent": 80},
      "status": {"battery": {"remainingCruisingRangeInMeters": 296000, "stateOfChargeInPercent": 75},
        "chargePowerInKw": 0.0, "fullyChargedAt": "…Z", "plugConnectionState": "DISCONNECTED",
        "plugLockState": "UNLOCKED", "remainingTimeToFullyChargedInMinutes": 0,
        "state": "CONNECT_CABLE"}},
    "chargingProfiles": {"profiles": [{"id": 1, "name": "Doma",
        "settings": {"autoUnlockPlugWhenCharged": "PERMANENT", "maxChargingCurrent": "MAXIMUM",
          "minBatteryStateOfCharge": {"minimumBatteryStateOfChargeInPercent": 30},
          "targetStateOfChargeInPercent": 50},
        "preferredChargingTimes": [{"id": 1, "enabled": false, "startTime": "22:00", "endTime": "06:00"}, "…×4"],
        "timers": [{"id": 1, "enabled": false, "type": "RECURRING",
          "recurringOn": ["MONDAY", "…"], "time": "07:00"}, "…×3"]}],
      "carCapturedTimestamp": "…Z",
      "currentVehiclePositionProfile": {"id": 1, "name": "Doma", "targetStateOfChargeInPercent": 50}},
    "licensePlate": "<redacted>", "name": "<redacted>",
    "odometer": {"mileageInKm": 8279, "carCapturedTimestamp": "…Z"},
    "operations": [{"name": "startCharging"}, {"name": "stopCharging"}, {"name": "setChargingLimit"},
      {"name": "setChargeMode"}, {"name": "updateChargingProfile"},
      {"name": "startAirConditioning"}, {"name": "stopAirConditioning"}],
    "parkingPosition": {"state": "PARKED", "formattedAddress": "<redacted>",
      "gpsCoordinates": {"latitude": 0, "longitude": 0}},
    "renderUrl": "<redacted>",
    "status": {"overall": {"doorsLocked": "YES", "locked": "YES", "doors": "CLOSED",
        "windows": "CLOSED", "lights": "OFF", "reliableLockStatus": "LOCKED"},
      "detail": {"sunroof": "UNSUPPORTED", "trunk": "CLOSED", "bonnet": "CLOSED"},
      "carCapturedTimestamp": "…Z"},
    "vin": "<VIN>"
  },
  "errors": [
    {"type": "FUEL_STATUS_UNSUPPORTED", "description": "Fuel status is not supported by the vehicle."},
    {"type": "AUXILIARY_HEATING_UNSUPPORTED", "description": "Auxiliary heating information is not supported by the vehicle."},
    {"type": "ACTIVE_VENTILATION_UNSUPPORTED", "description": "Active ventilation information is not supported by the vehicle."}
  ]
}
```

## Error bodies seen

```json
{"detail":"No static resource api/v1/vehicles.","instance":"/api/v1/vehicles","status":404,"title":"Not Found","type":"about:blank"}
{"detail":"The API key is not authorized to execute this operation.","instance":"/api/v1/vehicles/<VIN>?include=info","status":403,"title":"Forbidden","type":"https://public.api.connect.skoda-auto.cz/problems/api-key-not-authorized"}
```

## Not tested (deliberately)

- A second key on the same VIN. Per-VIN is documented; design for the worst case.
- A 401 for an expired or deleted key. The type is documented (`problems/api-key-expired`); not
  worth burning a key.
- A repeated start while AC is already on; 429 / `vehicle-not-accepting-requests`; 504.
