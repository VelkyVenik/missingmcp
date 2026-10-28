"""The Škoda brand client (adapters/car/skoda.py) against the fake MyŠkoda
Public API upstream — plus a few sanity checks pinning the fake itself."""
import httpx
from conftest import SKODA_KEY, SKODA_VIN


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
