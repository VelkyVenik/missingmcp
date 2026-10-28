"""The /car/mcp server: a hand-rolled, stateless, tools-only MCP (the same
JSON-RPC subset as adapters/whoop/mcp.py) with brand-neutral car tools.

The bmw brand (read-only, ~50 requests/24 h per Client ID) takes its own path,
_bmw_call: one telematic read is cached 15 min and serves status and location
alike, and every BMW call first makes sure the access token is fresh (rotating
refresh, persisted before use — bmw.BmwClient.ensure_fresh). The rest of this
docstring is the Škoda path.

Shaped by the upstream's 20 requests/hour per car (shared with the owner's
other keys): tools/list is filtered by the car's stored operations[] (no
upstream call), status reads are cached for 60 s, actions return as soon as
the upstream accepts them (no polling — a sleeping car takes ~2 min), and every
result reports the remaining budget. An upstream 401 (key expired or revoked)
raises SessionExpired so the proxy answers re-auth 401."""
from __future__ import annotations
import json
import math
import time
from datetime import datetime, timedelta, timezone
from ... import store
from ...log import log_warn
from ..base import SessionExpired
from collections import deque
from . import bmw, skoda
from .common import CarUpstreamError
from .skoda import Budget

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "missingmcp-car", "version": "1.0.0"}

STATUS_CACHE_S = 60
BMW_CACHE_S = 15 * 60
EXPIRY_NOTICE_DAYS = 14
TEMP_MIN_C, TEMP_MAX_C = 15, 30
CHARGE_LIMITS = (50, 60, 70, 80, 90, 100)
STATUS_INCLUDES = ["status", "charging", "airConditioning", "activeVentilation",
                   "odometer", "operations"]

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_SLOW = (" A sleeping car can take ~2 minutes to react; don't poll — check "
         "get_vehicle_status later if the user asks.")
_ACCEPTED = ("Command sent. A sleeping car can take ~2 minutes to react — "
             "check with get_vehicle_status later.")

# (name, description, input schema, required operationId or None, read-only).
# None = always listed. scripts/gen_car_tools.py renders the landing page's
# tool list from this.
TOOLS = [
    ("get_vehicle_capabilities",
     "What this car supports (available tools), its brand, key expiry and the request "
     "budget. Free: makes no request to the car.",
     _EMPTY, None, True),
    ("get_vehicle_status",
     "Current state: locks, doors, windows, battery %, range, charging, climate, "
     "ventilation, odometer, each with the time the car reported it. Counts against the "
     "car maker's request limit (see budget), so it's cached briefly. No location — see "
     "get_vehicle_location.",
     _EMPTY, None, True),
    ("get_vehicle_location",
     "Where the car is parked: address and GPS coordinates. Only call when the user asks "
     "where the car is.",
     _EMPTY, None, True),
    ("start_climate",
     "Start climate control (pre-heat or pre-cool the cabin)." + _SLOW,
     {"type": "object", "properties": {
         "target_temperature_c": {"type": "number", "minimum": TEMP_MIN_C,
                                  "maximum": TEMP_MAX_C,
                                  "description": "Target cabin temperature in °C; "
                                                 "omit to keep the car's setting"},
         "allow_battery": {"type": "boolean",
                           "description": "Allow running on the car's battery when it "
                                          "isn't plugged in"}},
      "additionalProperties": False},
     "startAirConditioning", False),
    ("stop_climate", "Stop climate control." + _SLOW, _EMPTY, "stopAirConditioning", False),
    ("start_ventilation", "Start cabin ventilation." + _SLOW, _EMPTY,
     "startActiveVentilation", False),
    ("stop_ventilation", "Stop cabin ventilation." + _SLOW, _EMPTY,
     "stopActiveVentilation", False),
    ("start_charging", "Start charging (the car must be plugged in)." + _SLOW, _EMPTY,
     "startCharging", False),
    ("stop_charging", "Stop charging." + _SLOW, _EMPTY, "stopCharging", False),
    ("set_charge_limit", "Set the target battery level charging stops at." + _SLOW,
     {"type": "object", "properties": {
         "percent": {"type": "integer", "enum": list(CHARGE_LIMITS),
                     "description": "Target state of charge in percent"}},
      "required": ["percent"], "additionalProperties": False},
     "setChargingLimit", False),
]

_CLIENTS = {"skoda": skoda.SkodaClient}

# CarUpstreamError.kind → tool-error text ({min} = minutes until the budget resets)
_ERRORS = {
    "rate_limited": "Škoda's limit of 20 requests/hour for this car is used up; it resets "
                    "in {min} min.",
    "car_busy": "The car isn't accepting commands right now (low battery or another "
                "operation running) — try again later.",
    "unsupported": "This car doesn't support that, or it's disabled (consent, licence or "
                   "workshop mode).",
    "car_timeout": "The car didn't respond in time — try again.",
    "vin_not_covered": "The stored key no longer covers this car — reconnect with a new key.",
    "upstream_error": "Škoda's service didn't answer — try again later.",
}

# The bmw brand's tool errors (invalid_key / container_missing re-auth instead)
_BMW_ERRORS = {
    "rate_limited": "BMW's daily limit (~50 requests per Client ID, shared with any other "
                    "app using it) is used up — try again tomorrow.",
    "vin_not_covered": "BMW refused access to this car — check that CarData API is still "
                       "ticked for your Client ID, or reconnect.",
    "upstream_error": "BMW's service didn't answer — try again later.",
}


def _http_json(status: int, obj) -> "tuple[int, dict, bytes]":
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()


def _result(rid, result) -> "tuple[int, dict, bytes]":
    return _http_json(200, {"jsonrpc": "2.0", "id": rid, "result": result})


def _rpc_error(rid, code: int, message: str) -> "tuple[int, dict, bytes]":
    return _http_json(200, {"jsonrpc": "2.0", "id": rid,
                            "error": {"code": code, "message": message}})


def _tool_error(rid, message: str) -> "tuple[int, dict, bytes]":
    return _result(rid, {"content": [{"type": "text", "text": message}], "isError": True})


def _tool_ok(rid, payload: dict) -> "tuple[int, dict, bytes]":
    return _result(rid, {"content": [{"type": "text", "text": json.dumps(payload)}],
                         "isError": False})


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _action_body(name: str, args: dict) -> dict | None:
    """Validate client arguments and build the upstream body; ValueError on bad input."""
    if name == "start_climate":
        body = {}
        if "target_temperature_c" in args:
            t = args["target_temperature_c"]
            if not _is_number(t) or not TEMP_MIN_C <= t <= TEMP_MAX_C:
                raise ValueError(f"target_temperature_c must be a number "
                                 f"{TEMP_MIN_C}–{TEMP_MAX_C}")
            body["targetTemperature"] = {"value": float(t), "unit": "CELSIUS"}
        if "allow_battery" in args:
            if not isinstance(args["allow_battery"], bool):
                raise ValueError("allow_battery must be true or false")
            body["airConditioningWithoutExternalPower"] = args["allow_battery"]
        return body or None
    if name == "set_charge_limit":
        p = args.get("percent")
        if isinstance(p, bool) or p not in CHARGE_LIMITS:
            raise ValueError(f"percent must be one of {', '.join(map(str, CHARGE_LIMITS))}")
        return {"targetStateOfChargeInPercent": int(p)}
    return None


class CarLocalForward:
    """LocalForward strategy for the car adapter: the whole MCP server, in-process.
    Budget and status cache are process-local (single-node by design)."""

    def __init__(self, config, now=None):
        self._cfg = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._budget: dict[str, Budget] = {}                  # VIN → last observed
        self._status_cache: dict[str, tuple[datetime, dict]] = {}   # "account|VIN" → (at, payload)
        self._bmw = bmw.BmwClient(config)
        self._bmw_calls: dict[str, deque] = {}        # VIN → monotonic times of our calls
        self._bmw_cache: dict[str, tuple[datetime, dict]] = {}      # "account|VIN" → (at, telematics)

    def _client(self, brand: str):
        return _CLIENTS[brand](self._cfg.car_skoda_api_base)

    async def handle(self, conn, account_key: str, blob: str,
                     body: bytes) -> "tuple[int, dict, bytes]":
        try:
            req = json.loads(body)
        except (ValueError, TypeError):
            req = None
        if not isinstance(req, dict):          # garbage or JSON-RPC batch
            return _http_json(400, {"error": "invalid_request"})
        method = req.get("method", "")
        rid = req.get("id")
        if rid is None:                        # notification: acknowledge, no body
            return 202, {"Content-Type": "application/json"}, b""
        if method == "initialize":
            params = req.get("params")
            params = params if isinstance(params, dict) else {}
            client_ver = params.get("protocolVersion", "")
            ver = client_ver if client_ver in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(rid, {"protocolVersion": ver, "capabilities": {"tools": {}},
                                 "serverInfo": SERVER_INFO})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            ops = set(json.loads(blob).get("operations") or [])
            return _result(rid, {"tools": [
                {"name": name, "description": desc, "inputSchema": schema,
                 "annotations": {"readOnlyHint": read_only}}
                for name, desc, schema, op, read_only in TOOLS if op is None or op in ops]})
        if method == "tools/call":
            params = req.get("params")
            if not isinstance(params, dict):
                return _rpc_error(rid, -32602, "params must be an object")
            return await self._call(conn, account_key, json.loads(blob), rid, params)
        return _rpc_error(rid, -32601, f"Method not found: {method}")

    # --- shared result decoration ---------------------------------------------

    def _note_budget(self, vin: str, budget: Budget | None) -> None:
        if budget is not None:
            self._budget[vin] = budget

    def _budget_view(self, vin: str) -> dict | None:
        b = self._budget.get(vin)
        if b is None:
            return None
        view = {"remaining": b.remaining}
        if b.reset_s is not None:
            left = max(0.0, b.reset_s - (time.monotonic() - b.at))
            view["resets_in_min"] = math.ceil(left / 60)
        return view

    def _decorate(self, d: dict, payload: dict) -> dict:
        budget = self._budget_view(d["vin"])
        if budget:
            payload["budget"] = budget
        expires = _parse_ts(d.get("expires_at"))
        if expires and self._now() >= expires - timedelta(days=EXPIRY_NOTICE_DAYS):
            payload["notice"] = (f"Your Škoda API key expires on {expires.date().isoformat()}. "
                                 "Create a new one in the MyŠkoda app (Profile → Smart "
                                 "home) and reconnect.")
        return payload

    # --- tools/call -------------------------------------------------------------

    async def _call(self, conn, account_key, d, rid, params):
        name = params.get("name", "")
        tool = next((t for t in TOOLS if t[0] == name), None)
        if tool is None:
            return _rpc_error(rid, -32602, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(rid, -32602, "arguments must be an object")
        op = tool[3]
        if op is not None and op not in (d.get("operations") or []):
            return _tool_error(rid, f"This car doesn't support {name}.")
        try:
            body = _action_body(name, args)
        except (ValueError, TypeError, OverflowError) as e:
            return _tool_error(rid, f"Invalid or missing argument: {e}")
        if d.get("brand") == "bmw":
            return await self._bmw_call(conn, account_key, d, rid, name)
        try:
            if name == "get_vehicle_capabilities":
                payload = self._capabilities(d)
            elif name == "get_vehicle_status":
                payload = await self._status(conn, account_key, d)
            elif name == "get_vehicle_location":
                reply = await self._client(d["brand"]).read(d["api_key"], d["vin"],
                                                            ["parkingPosition"])
                self._note_budget(d["vin"], reply.budget)
                payload = skoda.normalize_location((reply.data or {}).get("vehicle", {}))
            else:
                reply = await self._client(d["brand"]).action(d["api_key"], d["vin"], op, body)
                self._note_budget(d["vin"], reply.budget)
                self._status_cache.pop(f"{account_key}|{d['vin']}", None)
                payload = {"accepted": True, "message": _ACCEPTED}
        except CarUpstreamError as e:
            if e.kind == "invalid_key":
                raise SessionExpired(str(e)) from e
            log_warn("car-upstream-error", account=account_key, tool=name,
                     status=e.status, problem=e.problem)
            budget = self._budget_view(d["vin"]) or {}
            minutes = math.ceil(e.retry_after_s / 60) if e.retry_after_s else \
                budget.get("resets_in_min", 60)
            return _tool_error(rid, _ERRORS.get(e.kind, _ERRORS["upstream_error"])
                               .format(min=minutes))
        return _tool_ok(rid, self._decorate(d, payload))

    def _capabilities(self, d: dict) -> dict:
        ops = set(d.get("operations") or [])
        return {"vehicle": {"brand": d["brand"], "vin_tail": d["vin"][-6:]},
                "available_tools": [t[0] for t in TOOLS if t[3] is None or t[3] in ops],
                "key_expires_at": d.get("expires_at")}

    # --- the bmw brand ------------------------------------------------------------

    async def _bmw_call(self, conn, account_key, d, rid, name):
        try:
            if name == "get_vehicle_capabilities":
                payload = self._capabilities(d)
            else:
                td, cached = await self._bmw_telematics(conn, account_key, d)
                payload = (bmw.normalize_status(td) if name == "get_vehicle_status"
                           else bmw.normalize_location(td))
                if cached:
                    payload["cached"] = True
        except CarUpstreamError as e:
            # 401 after a fresh token, or our container gone: a reconnect redoes
            # the sign-in, which mints tokens and recreates the container.
            if e.kind in ("invalid_key", "container_missing"):
                raise SessionExpired(str(e)) from e
            log_warn("car-upstream-error", account=account_key, tool=name,
                     status=e.status, problem=e.problem)
            return _tool_error(rid, _BMW_ERRORS.get(e.kind, _BMW_ERRORS["upstream_error"]))
        payload["budget"] = self._bmw_budget(d["vin"])
        return _tool_ok(rid, payload)

    async def _bmw_telematics(self, conn, account_key: str, d: dict) -> "tuple[dict, bool]":
        now = self._now()
        cache_key = f"{account_key}|{d['vin']}"
        hit = self._bmw_cache.get(cache_key)
        if hit and (now - hit[0]).total_seconds() < BMW_CACHE_S:
            return hit[1], True
        d = await self._bmw.ensure_fresh(conn, account_key, d)
        self._bmw_calls.setdefault(d["vin"], deque()).append(time.monotonic())
        td = await self._bmw.telematics(d)
        self._bmw_cache[cache_key] = (now, td)
        return td, False

    def _bmw_budget(self, vin: str) -> dict:
        """Our own count: BMW's quota is per Client ID and other apps spend it too,
        and the count is process-local (a deploy resets it)."""
        calls = self._bmw_calls.setdefault(vin, deque())
        while calls and time.monotonic() - calls[0] > 24 * 3600:
            calls.popleft()
        return {"used_last_24h_by_missingmcp": len(calls), "daily_limit": bmw.DAILY_LIMIT,
                "note": "BMW's limit is per Client ID, shared with any other app using it; "
                        "data is cached for 15 min."}

    async def _status(self, conn, account_key: str, d: dict) -> dict:
        now = self._now()
        cache_key = f"{account_key}|{d['vin']}"      # a reconnect may switch the car
        hit = self._status_cache.get(cache_key)
        if hit and (now - hit[0]).total_seconds() < STATUS_CACHE_S:
            return {**hit[1], "cached": True}
        reply = await self._client(d["brand"]).read(d["api_key"], d["vin"], STATUS_INCLUDES)
        self._note_budget(d["vin"], reply.budget)
        data = reply.data or {}
        vehicle = data.get("vehicle", {})
        if "operations" in vehicle:
            self._refresh_operations(conn, account_key, d, skoda.operations(vehicle))
        payload = skoda.normalize_status(vehicle, data.get("errors", []), d["vin"])
        self._status_cache[cache_key] = (now, payload)
        return dict(payload)

    def _refresh_operations(self, conn, account_key: str, d: dict, ops: list[str]) -> None:
        """Compare-and-set: a reconnect may have replaced the blob while the read
        awaited the upstream, so re-read the stored blob and only touch it if it
        still holds the same credential. No await between read and write — atomic
        on the single event loop, which also runs oauth._finish."""
        raw = store.get_account_tokens(conn, "car", account_key, self._cfg.gateway_secret)
        if raw is None:
            return
        current = json.loads(raw)
        if (current.get("api_key"), current.get("vin")) != (d["api_key"], d["vin"]):
            return
        if current.get("operations") != ops:
            store.upsert_account(conn, "car", account_key,
                                 json.dumps({**current, "operations": ops}),
                                 self._cfg.gateway_secret)
