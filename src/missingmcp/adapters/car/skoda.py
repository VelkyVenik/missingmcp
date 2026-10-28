"""The `skoda` brand of the car connector: a client for the official MyŠkoda
Public API (contract 1.1.0, https://public.api.connect.skoda-auto.cz/docs).

Auth is a user-owned `X-API-Key` (created in the MyŠkoda app, ~6-month life),
bound to the VINs picked at creation. The budget is 20 requests/hour per VIN,
shared by every key on that car, so this module never retries: each call is
exactly one upstream request. Errors are application/problem+json, told apart
by the `type` tail (two different 429s share the status).

Shared pieces (CarUpstreamError, normalize_vin) live in common.py; bmw.py is the
second brand."""
from __future__ import annotations
import time
from dataclasses import dataclass
import httpx
from .common import CarUpstreamError, normalize_vin  # noqa: F401 (re-exported)

PROBLEM_PREFIX = "/problems/"

# operationId → (HTTP method, path tail under /api/v1/vehicles/{vin})
ACTIONS = {
    "startAirConditioning": ("POST", "air-conditioning/start"),
    "stopAirConditioning": ("POST", "air-conditioning/stop"),
    "startActiveVentilation": ("POST", "active-ventilation/start"),
    "stopActiveVentilation": ("POST", "active-ventilation/stop"),
    "startCharging": ("POST", "charging/start"),
    "stopCharging": ("POST", "charging/stop"),
    "setChargingLimit": ("PUT", "charging/limit"),
}


@dataclass
class Budget:
    remaining: int | None
    reset_s: int | None
    at: float                      # time.monotonic() when observed


@dataclass
class Reply:
    data: dict | None              # parsed JSON for reads, None for 202 actions
    budget: Budget | None
    expires_at: str | None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _budget(r: httpx.Response) -> Budget | None:
    remaining = _int(r.headers.get("RateLimit-Remaining"))
    if remaining is None:
        return None
    return Budget(remaining, _int(r.headers.get("RateLimit-Reset")), time.monotonic())


def _problem_tail(r: httpx.Response) -> str:
    try:
        ptype = r.json().get("type", "")
    except ValueError:
        return ""
    return ptype.rsplit(PROBLEM_PREFIX, 1)[-1] if PROBLEM_PREFIX in ptype else ptype


def _error(r: httpx.Response) -> CarUpstreamError:
    s, tail = r.status_code, _problem_tail(r)
    if s == 401:
        kind = "invalid_key"
    elif s == 403:
        kind = "vin_not_covered"
    elif s == 422:
        kind = "unsupported"
    elif s == 429:
        kind = "car_busy" if tail == "vehicle-not-accepting-requests" else "rate_limited"
    elif s == 504:
        kind = "car_timeout"
    else:
        kind = "upstream_error"
    return CarUpstreamError(kind, s, tail, _int(r.headers.get("Retry-After")))


def _reply(r: httpx.Response) -> Reply:
    if r.status_code >= 400:
        raise _error(r)
    data = r.json() if r.status_code == 200 and r.content else None
    return Reply(data, _budget(r), r.headers.get("X-API-Key-Expires-At"))


# --- brand-neutral shapes -----------------------------------------------------
# Škoda's enum values are passed through lower-cased (CONNECT_CABLE →
# "connect_cable"): mechanical, no invented semantics. Identity and location
# fields (name, licensePlate, renderUrl, parkingPosition, the full VIN) never
# enter the status shape — location has its own tool.

# errors[] type → neutral section name reported under "unsupported"
_UNSUPPORTED = {
    "STATUS_UNSUPPORTED": "status",
    "FUEL_STATUS_UNSUPPORTED": "fuel",
    "ODOMETER_UNSUPPORTED": "odometer",
    "PARKING_POSITION_UNSUPPORTED": "location",
    "AIR_CONDITIONING_UNSUPPORTED": "climate",
    "AUXILIARY_HEATING_UNSUPPORTED": "auxiliary_heating",
    "ACTIVE_VENTILATION_UNSUPPORTED": "ventilation",
    "CHARGING_UNSUPPORTED": "charging",
    "CHARGING_PROFILES_UNSUPPORTED": "charging_profiles",
}


def _low(value):
    return value.lower() if isinstance(value, str) else value


def _yes(value) -> bool | None:
    return {"YES": True, "NO": False}.get(value)


def normalize_status(vehicle: dict, errors: list, vin: str) -> dict:
    unsupported = [_UNSUPPORTED[e.get("type")] for e in errors or []
                   if e.get("type") in _UNSUPPORTED]
    out: dict = {"vehicle": {"brand": "skoda", "vin_tail": normalize_vin(vin)[-6:]}}

    st = vehicle.get("status") if "status" not in unsupported else None
    if st:
        overall, detail = st.get("overall", {}), st.get("detail", {})
        out["locks"] = {"locked": _yes(overall.get("locked")),
                        "doors_locked": _yes(overall.get("doorsLocked")),
                        "captured_at": st.get("carCapturedTimestamp")}
        for key, src in (("doors", overall), ("windows", overall), ("lights", overall),
                         ("trunk", detail), ("bonnet", detail)):
            if key in src:
                out[key] = _low(src[key])

    ch = vehicle.get("charging") if "charging" not in unsupported else None
    if ch:
        status, settings = ch.get("status", {}), ch.get("settings", {})
        battery = status.get("battery", {})
        if "stateOfChargeInPercent" in battery:
            out["battery_percent"] = battery["stateOfChargeInPercent"]
        if "remainingCruisingRangeInMeters" in battery:
            out["range_km"] = round(battery["remainingCruisingRangeInMeters"] / 1000)
        out["charging"] = {"state": _low(status.get("state")),
                           "plug": _low(status.get("plugConnectionState")),
                           "power_kw": status.get("chargePowerInKw"),
                           "limit_percent": settings.get("targetStateOfChargeInPercent"),
                           "minutes_to_full": status.get("remainingTimeToFullyChargedInMinutes"),
                           "captured_at": ch.get("carCapturedTimestamp")}

    ac = vehicle.get("airConditioning") if "climate" not in unsupported else None
    if ac:
        climate = {"state": _low(ac.get("state")),
                   "target_c": (ac.get("targetTemperature") or {}).get("value"),
                   "captured_at": ac.get("carCapturedTimestamp")}
        wh = ac.get("windowHeating")
        if wh:
            climate["window_heating"] = {"front": _low(wh.get("front")),
                                         "rear": _low(wh.get("rear"))}
        if ac.get("estimatedReachOfTargetTemperatureAt"):
            climate["target_reached_at"] = ac["estimatedReachOfTargetTemperatureAt"]
        out["climate"] = climate

    vent = vehicle.get("activeVentilation") if "ventilation" not in unsupported else None
    if vent:
        out["ventilation"] = {"state": _low(vent.get("state")),
                              "captured_at": vent.get("carCapturedTimestamp")}

    odo = vehicle.get("odometer") if "odometer" not in unsupported else None
    if odo and "mileageInKm" in odo:
        out["odometer_km"] = odo["mileageInKm"]

    if unsupported:
        out["unsupported"] = unsupported
    return out


def normalize_location(vehicle: dict) -> dict:
    pos = vehicle.get("parkingPosition") or {}
    gps = pos.get("gpsCoordinates") or {}
    return {"parked": pos.get("state") == "PARKED" if pos.get("state") else None,
            "address": pos.get("formattedAddress"),
            "lat": gps.get("latitude"), "lon": gps.get("longitude"),
            "captured_at": pos.get("carCapturedTimestamp")}


def operations(vehicle: dict) -> list[str]:
    """The car's supported operationIds from an `include=operations` read."""
    return [o["name"] for o in vehicle.get("operations") or [] if isinstance(o, dict)
            and isinstance(o.get("name"), str)]


class SkodaClient:
    def __init__(self, base_url: str, timeout_s: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _url(self, vin: str, tail: str = "") -> str:
        return f"{self.base_url}/api/v1/vehicles/{normalize_vin(vin)}" + (f"/{tail}" if tail else "")

    @staticmethod
    def _headers(api_key: str) -> dict:
        return {"X-API-Key": api_key, "Accept": "application/json"}

    def probe_sync(self, api_key: str, vin: str) -> Reply:
        """Sign-in check (blocking; callers run it under oauth._bounded): one GET
        for the car's operations, which also proves the key covers the VIN."""
        url = self._url(vin)
        try:
            r = httpx.get(url, params={"include": "operations"},
                          headers=self._headers(api_key), timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        return _reply(r)

    async def read(self, api_key: str, vin: str, includes: list[str]) -> Reply:
        url = self._url(vin)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                # literal commas: the contract documents ?include=a,b
                r = await c.get(f"{url}?include={','.join(includes)}",
                                headers=self._headers(api_key))
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        return _reply(r)

    async def action(self, api_key: str, vin: str, op: str, body: dict | None) -> Reply:
        if op not in ACTIONS:
            raise ValueError(f"unknown operation: {op}")
        method, tail = ACTIONS[op]
        url = self._url(vin, tail)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.request(method, url, headers=self._headers(api_key),
                                    json=body)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        return _reply(r)
