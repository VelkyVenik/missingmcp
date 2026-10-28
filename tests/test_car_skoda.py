"""The Škoda brand client (adapters/car/skoda.py) against the fake MyŠkoda
Public API upstream — plus a few sanity checks pinning the fake itself."""
import asyncio
import json
import httpx
import pytest
from conftest import SKODA_KEY, SKODA_VIN
from missingmcp.adapters.car.skoda import CarUpstreamError, SkodaClient


def _url(fake, tail=""):
    return f"http://127.0.0.1:{fake.port}/api/v1/vehicles/{SKODA_VIN}{tail}"


# --- fake upstream sanity ------------------------------------------------------

def test_fake_unknown_key_is_401_and_not_counted(fake_skoda):
    r = httpx.get(_url(fake_skoda), headers={"X-API-Key": "nope"})
    assert r.status_code == 401 and r.json()["type"] == "about:blank"
    assert fake_skoda.remaining == 20


def test_fake_foreign_vin_is_403_and_not_counted(fake_skoda):
    r = httpx.get(f"http://127.0.0.1:{fake_skoda.port}/api/v1/vehicles/TMBTEST0000000009",
                  headers={"X-API-Key": SKODA_KEY})
    assert r.status_code == 403
    assert r.json()["type"].endswith("/problems/api-key-not-authorized")
    assert fake_skoda.remaining == 20


def test_fake_include_filtering_and_unsupported(fake_skoda):
    r = httpx.get(_url(fake_skoda, "?include=charging,activeVentilation"),
                  headers={"X-API-Key": SKODA_KEY})
    body = r.json()
    assert set(body["vehicle"]) == {"charging"}
    assert body["errors"][0]["type"] == "ACTIVE_VENTILATION_UNSUPPORTED"
    assert r.headers["RateLimit-Remaining"] == "19"
    assert r.headers["X-API-Key-Expires-At"] == fake_skoda.expires_at


def test_fake_action_changes_next_read(fake_skoda):
    h = {"X-API-Key": SKODA_KEY}
    r = httpx.post(_url(fake_skoda, "/air-conditioning/start"), headers=h, json={})
    assert r.status_code == 202 and r.content == b""
    ac = httpx.get(_url(fake_skoda, "?include=airConditioning"), headers=h).json()
    assert ac["vehicle"]["airConditioning"]["state"] == "HEATING"
    assert fake_skoda.remaining == 18


# --- SkodaClient ----------------------------------------------------------------

def _client(fake):
    return SkodaClient(f"http://127.0.0.1:{fake.port}", timeout_s=2.0)


def test_probe_sync_returns_operations_expiry_and_budget(fake_skoda):
    reply = _client(fake_skoda).probe_sync(SKODA_KEY, SKODA_VIN)
    ops = [o["name"] for o in reply.data["vehicle"]["operations"]]
    assert "startAirConditioning" in ops
    assert reply.expires_at == fake_skoda.expires_at
    assert reply.budget.remaining == 19 and reply.budget.reset_s == 3600
    method, path, headers, _ = fake_skoda.calls[-1]
    assert (method, path) == ("GET", f"/api/v1/vehicles/{SKODA_VIN}?include=operations")
    assert headers["X-API-Key"] == SKODA_KEY


def test_probe_sync_uppercases_and_strips_vin(fake_skoda):
    _client(fake_skoda).probe_sync(SKODA_KEY, f"  {SKODA_VIN.lower()} ")
    assert SKODA_VIN in fake_skoda.calls[-1][1]


@pytest.mark.parametrize("vin", ["SHORT", "TMBTEST000000000I", "TMBTEST00000000/1"])
def test_invalid_vin_rejected_before_any_request(fake_skoda, vin):
    with pytest.raises(CarUpstreamError) as e:
        _client(fake_skoda).probe_sync(SKODA_KEY, vin)
    assert e.value.kind == "invalid_vin"
    assert fake_skoda.calls == []


def test_read_passes_includes(fake_skoda):
    reply = asyncio.run(_client(fake_skoda).read(SKODA_KEY, SKODA_VIN,
                                                 ["charging", "odometer"]))
    assert set(reply.data["vehicle"]) == {"charging", "odometer"}
    assert fake_skoda.calls[-1][1].endswith("?include=charging,odometer")


@pytest.mark.parametrize("op,method,tail,body", [
    ("startAirConditioning", "POST", "/air-conditioning/start",
     {"targetTemperature": {"value": 21.0, "unit": "CELSIUS"}}),
    ("stopAirConditioning", "POST", "/air-conditioning/stop", None),
    ("startActiveVentilation", "POST", "/active-ventilation/start", None),
    ("stopActiveVentilation", "POST", "/active-ventilation/stop", None),
    ("startCharging", "POST", "/charging/start", None),
    ("stopCharging", "POST", "/charging/stop", None),
    ("setChargingLimit", "PUT", "/charging/limit", {"targetStateOfChargeInPercent": 90}),
])
def test_action_routes(fake_skoda, op, method, tail, body):
    reply = asyncio.run(_client(fake_skoda).action(SKODA_KEY, SKODA_VIN, op, body))
    assert reply.data is None and reply.budget.remaining == 19
    m, path, _, sent = fake_skoda.calls[-1]
    assert (m, path) == (method, f"/api/v1/vehicles/{SKODA_VIN}{tail}")
    assert (json.loads(sent) if sent else None) == body


def test_unknown_action_rejected(fake_skoda):
    with pytest.raises(ValueError):
        asyncio.run(_client(fake_skoda).action(SKODA_KEY, SKODA_VIN, "honk", None))


@pytest.mark.parametrize("force,kind", [
    ((401, "api-key-expired"), "invalid_key"),
    ((401, "about:blank"), "invalid_key"),
    ((403, "api-key-not-authorized"), "vin_not_covered"),
    ((422, "operation-not-supported"), "unsupported"),
    ((422, "operation-disabled"), "unsupported"),
    ((429, "rate-limit-exceeded"), "rate_limited"),
    ((429, "vehicle-not-accepting-requests"), "car_busy"),
    ((504, "gateway-timeout"), "car_timeout"),
    ((500, "internal"), "upstream_error"),
])
def test_error_mapping(fake_skoda, force, kind):
    fake_skoda.force = force
    with pytest.raises(CarUpstreamError) as e:
        asyncio.run(_client(fake_skoda).read(SKODA_KEY, SKODA_VIN, ["status"]))
    assert e.value.kind == kind and e.value.status == force[0]
    assert SKODA_KEY not in str(e.value) and SKODA_VIN not in str(e.value)
    if kind == "rate_limited":
        assert e.value.retry_after_s == 1200


def test_sync_error_mapping(fake_skoda):
    with pytest.raises(CarUpstreamError) as e:
        _client(fake_skoda).probe_sync("wrong-key", SKODA_VIN)
    assert e.value.kind == "invalid_key"


def test_connect_error_is_upstream_error():
    c = SkodaClient("http://127.0.0.1:9", timeout_s=0.5)
    with pytest.raises(CarUpstreamError) as e:
        asyncio.run(c.read(SKODA_KEY, SKODA_VIN, ["status"]))
    assert e.value.kind == "upstream_error"
