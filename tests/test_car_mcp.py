"""The /car/mcp server (adapters/car/mcp.py) driven directly through
CarLocalForward.handle against the fake Škoda upstream."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
import pytest
from conftest import SKODA_KEY, SKODA_VIN
from missingmcp import store
from missingmcp.adapters.base import SessionExpired
from missingmcp.adapters.car.mcp import CarLocalForward, TOOLS
from missingmcp.config import load_config

ACCOUNT = "me@example.com"
EV_OPS = ["startCharging", "stopCharging", "setChargingLimit", "setChargeMode",
          "updateChargingProfile", "startAirConditioning", "stopAirConditioning"]
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _setup(fake, ops=None, expires_at="2027-04-26T09:11:23.249Z"):
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "CAR_SKODA_API_BASE": f"http://127.0.0.1:{fake.port}"})
    conn = store.init_db(":memory:")
    blob = json.dumps({"v": 1, "brand": "skoda", "api_key": SKODA_KEY, "vin": SKODA_VIN,
                       "expires_at": expires_at,
                       "operations": EV_OPS if ops is None else ops})
    store.upsert_account(conn, "car", ACCOUNT, blob, cfg.gateway_secret)
    fwd = CarLocalForward(cfg, now=lambda: NOW)
    return fwd, conn, blob, cfg


def _rpc(fwd, conn, blob, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "method": method, "id": rid}
    if params is not None:
        body["params"] = params
    status, _, payload = asyncio.run(fwd.handle(conn, ACCOUNT, blob, json.dumps(body).encode()))
    return status, json.loads(payload) if payload else None


def _call(fwd, conn, blob, name, args=None):
    _, resp = _rpc(fwd, conn, blob, "tools/call", {"name": name, "arguments": args or {}})
    res = resp["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


def _upstream_calls(fake):
    return [(m, p) for m, p, _, _ in fake.calls]


# --- protocol -------------------------------------------------------------------

def test_initialize_ping_notification(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _, r = _rpc(fwd, conn, blob, "initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["serverInfo"]["name"] == "missingmcp-car"
    assert _rpc(fwd, conn, blob, "ping")[1]["result"] == {}
    status, _, _ = asyncio.run(fwd.handle(conn, ACCOUNT, blob,
                                          b'{"jsonrpc":"2.0","method":"notifications/initialized"}'))
    assert status == 202
    assert fake_skoda.calls == []


def test_tools_list_filtered_by_operations_with_read_only_hints(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _, r = _rpc(fwd, conn, blob, "tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {"get_vehicle_capabilities", "get_vehicle_status",
                          "get_vehicle_location", "start_climate", "stop_climate",
                          "start_charging", "stop_charging", "set_charge_limit"}
    assert tools["get_vehicle_status"]["annotations"]["readOnlyHint"] is True
    assert tools["start_climate"]["annotations"]["readOnlyHint"] is False
    assert fake_skoda.calls == []


def test_tools_list_shows_ventilation_when_supported(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda, ops=EV_OPS + ["startActiveVentilation",
                                                           "stopActiveVentilation"])
    _, r = _rpc(fwd, conn, blob, "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    assert {"start_ventilation", "stop_ventilation"} <= names


def test_every_tool_is_in_the_table():
    assert {t[0] for t in TOOLS} == {
        "get_vehicle_capabilities", "get_vehicle_status", "get_vehicle_location",
        "start_climate", "stop_climate", "start_ventilation", "stop_ventilation",
        "start_charging", "stop_charging", "set_charge_limit"}


# --- reads ----------------------------------------------------------------------

def test_capabilities_costs_nothing(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, out = _call(fwd, conn, blob, "get_vehicle_capabilities")
    assert not err
    assert out["vehicle"] == {"brand": "skoda", "vin_tail": SKODA_VIN[-6:]}
    assert "start_climate" in out["available_tools"]
    assert "start_ventilation" not in out["available_tools"]
    assert out["key_expires_at"] == "2027-04-26T09:11:23.249Z"
    assert fake_skoda.calls == []


def test_status_reads_and_reports_budget(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, out = _call(fwd, conn, blob, "get_vehicle_status")
    assert not err
    assert out["battery_percent"] == 75 and out["climate"]["state"] == "off"
    assert out["budget"] == {"remaining": 19, "resets_in_min": 60}
    method, path = _upstream_calls(fake_skoda)[0]
    assert method == "GET" and "include=status,charging,airConditioning," \
        "activeVentilation,odometer,operations" in path
    # capabilities now knows the budget for free
    _, caps = _call(fwd, conn, blob, "get_vehicle_capabilities")
    assert caps["budget"]["remaining"] == 19


def test_status_never_leaks_location_or_identity(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _, out = _call(fwd, conn, blob, "get_vehicle_status")
    dumped = json.dumps(out)
    for s in ("PLATE-SENTINEL", "NAME-SENTINEL", "ADDR-SENTINEL", "49.123456", SKODA_VIN):
        assert s not in dumped


def test_location_only_in_location_tool(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, out = _call(fwd, conn, blob, "get_vehicle_location")
    assert not err and out["address"] == "ADDR-SENTINEL" and out["lat"] == 49.123456
    assert "include=parkingPosition" in _upstream_calls(fake_skoda)[0][1]


def test_status_cached_for_60s_and_action_invalidates(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _call(fwd, conn, blob, "get_vehicle_status")
    _, second = _call(fwd, conn, blob, "get_vehicle_status")
    assert second["cached"] is True
    assert len(fake_skoda.calls) == 1
    _call(fwd, conn, blob, "stop_climate")
    _, third = _call(fwd, conn, blob, "get_vehicle_status")
    assert "cached" not in third
    assert len(fake_skoda.calls) == 3


def test_status_cache_expires(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    clock = [NOW]
    fwd._now = lambda: clock[0]
    _call(fwd, conn, blob, "get_vehicle_status")
    clock[0] = NOW + timedelta(seconds=61)
    _, out = _call(fwd, conn, blob, "get_vehicle_status")
    assert "cached" not in out and len(fake_skoda.calls) == 2


def test_operations_refresh_never_overwrites_a_concurrent_reconnect(fake_skoda):
    """Codex P2: a reconnect lands while a status read is in flight; the read's
    operations refresh must not write the old key back over the new one."""
    fwd, conn, blob, cfg = _setup(fake_skoda, ops=["startCharging"])
    new_blob = {**json.loads(blob), "api_key": "key-new", "operations": ["startCharging"]}
    real_client = fwd._client

    class Racing:
        def __init__(self, inner):
            self.inner = inner

        async def read(self, *a, **kw):
            reply = await self.inner.read(*a, **kw)
            # the reconnect's _finish runs on the loop while we awaited upstream
            store.upsert_account(conn, "car", ACCOUNT, json.dumps(new_blob), cfg.gateway_secret)
            return reply

    fwd._client = lambda brand: Racing(real_client(brand))
    _call(fwd, conn, blob, "get_vehicle_status")
    stored = json.loads(store.get_account_tokens(conn, "car", ACCOUNT, cfg.gateway_secret))
    assert stored["api_key"] == "key-new"
    assert stored["operations"] == ["startCharging"]


def test_status_cache_is_per_car(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _call(fwd, conn, blob, "get_vehicle_status")
    fake_skoda.keys[SKODA_KEY].add("TMBTEST0000000002")
    other = json.dumps({**json.loads(blob), "vin": "TMBTEST0000000002"})
    _, out = _call(fwd, conn, other, "get_vehicle_status")
    assert "cached" not in out and out["vehicle"]["vin_tail"] == "000002"


def test_status_refreshes_stored_operations(fake_skoda):
    fwd, conn, blob, cfg = _setup(fake_skoda, ops=["startCharging"])
    _call(fwd, conn, blob, "get_vehicle_status")
    stored = json.loads(store.get_account_tokens(conn, "car", ACCOUNT, cfg.gateway_secret))
    assert stored["operations"] == EV_OPS
    assert stored["api_key"] == SKODA_KEY        # rest of the blob intact


# --- actions --------------------------------------------------------------------

def test_start_climate_sends_body_and_returns_at_once(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, out = _call(fwd, conn, blob, "start_climate",
                     {"target_temperature_c": 21, "allow_battery": True})
    assert not err and out["accepted"] is True and "2 minutes" in out["message"]
    assert out["budget"]["remaining"] == 19
    m, p, _, body = fake_skoda.calls[-1]
    assert (m, p) == ("POST", f"/api/v1/vehicles/{SKODA_VIN}/air-conditioning/start")
    assert json.loads(body) == {"targetTemperature": {"value": 21.0, "unit": "CELSIUS"},
                                "airConditioningWithoutExternalPower": True}
    assert len(fake_skoda.calls) == 1            # no polling


def test_start_climate_without_args_sends_no_body(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _call(fwd, conn, blob, "start_climate")
    assert fake_skoda.calls[-1][3] == b""


@pytest.mark.parametrize("tool,method,tail", [
    ("stop_climate", "POST", "/air-conditioning/stop"),
    ("start_charging", "POST", "/charging/start"),
    ("stop_charging", "POST", "/charging/stop"),
])
def test_simple_actions(fake_skoda, tool, method, tail):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, _ = _call(fwd, conn, blob, tool)
    assert not err
    assert _upstream_calls(fake_skoda)[-1] == (method, f"/api/v1/vehicles/{SKODA_VIN}{tail}")


def test_set_charge_limit(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, _ = _call(fwd, conn, blob, "set_charge_limit", {"percent": 90})
    assert not err
    assert json.loads(fake_skoda.calls[-1][3]) == {"targetStateOfChargeInPercent": 90}


@pytest.mark.parametrize("tool,args", [
    ("set_charge_limit", {"percent": 85}),
    ("set_charge_limit", {"percent": "ninety"}),
    ("set_charge_limit", {}),
    ("start_climate", {"target_temperature_c": 45}),
    ("start_climate", {"target_temperature_c": "warm"}),
    ("start_climate", {"allow_battery": "yes"}),
])
def test_invalid_args_make_no_upstream_call(fake_skoda, tool, args):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, text = _call(fwd, conn, blob, tool, args)
    assert err and fake_skoda.calls == []


def test_hidden_tool_is_a_tool_error(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    err, text = _call(fwd, conn, blob, "start_ventilation")
    assert err and "doesn't support" in text and fake_skoda.calls == []


def test_unknown_tool_is_rpc_error(fake_skoda):
    fwd, conn, blob, _ = _setup(fake_skoda)
    _, r = _rpc(fwd, conn, blob, "tools/call", {"name": "honk", "arguments": {}})
    assert r["error"]["code"] == -32602


# --- errors, expiry -------------------------------------------------------------

@pytest.mark.parametrize("force,needle", [
    ((429, "rate-limit-exceeded"), "20 requests/hour"),
    ((429, "vehicle-not-accepting-requests"), "isn't accepting commands"),
    ((422, "operation-not-supported"), "doesn't support"),
    ((504, "gateway-timeout"), "didn't respond"),
    ((500, "internal"), "didn't answer"),
])
def test_upstream_errors_are_tool_errors(fake_skoda, force, needle):
    fwd, conn, blob, _ = _setup(fake_skoda)
    fake_skoda.force = force
    err, text = _call(fwd, conn, blob, "start_climate")
    assert err and needle in text
    if force == (429, "rate-limit-exceeded"):
        assert "20 min" in text


@pytest.mark.parametrize("tail", ["api-key-expired", "about:blank"])
def test_401_raises_session_expired(fake_skoda, tail):
    fwd, conn, blob, _ = _setup(fake_skoda)
    fake_skoda.force = (401, tail)
    with pytest.raises(SessionExpired):
        _call(fwd, conn, blob, "get_vehicle_status")


def test_expiry_notice_within_14_days(fake_skoda):
    soon = (NOW + timedelta(days=13)).isoformat().replace("+00:00", "Z")
    fwd, conn, blob, _ = _setup(fake_skoda, expires_at=soon)
    _, out = _call(fwd, conn, blob, "get_vehicle_capabilities")
    assert "expires on" in out["notice"] and "MyŠkoda" in out["notice"]


def test_no_expiry_notice_beyond_14_days(fake_skoda):
    later = (NOW + timedelta(days=15)).isoformat().replace("+00:00", "Z")
    fwd, conn, blob, _ = _setup(fake_skoda, expires_at=later)
    _, out = _call(fwd, conn, blob, "get_vehicle_capabilities")
    assert "notice" not in out
