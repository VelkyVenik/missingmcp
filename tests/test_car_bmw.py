"""The car connector's bmw brand (BMW CarData) end to end through build_app — the
two-step device sign-in, the read-only tools, the shared 15-min telematic cache,
rotating token refresh — plus BmwClient's refresh lock, against the fake CarData
upstream."""
import asyncio
import base64
import hashlib
import html
import json
import re
import time
from urllib.parse import parse_qs, urlparse
from starlette.testclient import TestClient
from conftest import BMW_CLIENT_ID, BMW_LAT, BMW_LON, BMW_VIN
from missingmcp import store
from missingmcp.adapters.car import bmw
from missingmcp.app import build_app
from missingmcp.config import load_config

EMAIL = "Driver@Example.com"
KEY = "driver@example.com"


SECRET = "s" * 40


def _cfg(fake, db_path=":memory:"):
    base = f"http://127.0.0.1:{fake.port}"
    return load_config({"GATEWAY_SECRET": SECRET, "PUBLIC_URL": "https://gw.example.com",
                        "DB_PATH": str(db_path), "DATA_DIR": "/tmp",
                        "CAR_BMW_AUTH_BASE": base, "CAR_BMW_API_BASE": base})


def _client(fake, tmp_path):
    """The app on a file DB, so a test can open a second connection to it."""
    db = tmp_path / "gw.db"
    return TestClient(build_app(_cfg(fake, db))), db


def _hidden(page: str) -> dict:
    return {n: html.unescape(v) for n, v in
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)}


def _start(client, **fields):
    reg = client.post("/car/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    page = client.get("/car/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "claude-state", "code_challenge": challenge,
        "code_challenge_method": "S256"}).text
    data = {**_hidden(page), "car_brand": "bmw", "car_email": EMAIL,
            "car_bmw_client_id": BMW_CLIENT_ID, "car_bmw_vin": BMW_VIN, **fields}
    return client.post("/car/oauth/authorize", data=data, follow_redirects=False), reg, verifier


def _approve(client, page):
    return client.post("/car/oauth/authorize", data=_hidden(page), follow_redirects=False)


def _connect(client, fake):
    r, reg, verifier = _start(client)
    assert r.status_code == 200, r.text
    r = _approve(client, r.text)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    r = client.post("/car/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert r.status_code == 200
    return r.json()["access_token"]


def _call(client, token, name, arguments=None):
    return client.post("/car/mcp", headers={"Authorization": f"Bearer {token}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments or {}}})


def _payload(r) -> dict:
    result = r.json()["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _raw_blob(db):
    return store.get_account_tokens(store.init_db(str(db)), "car", KEY, SECRET)


def _blob(db) -> dict:
    return json.loads(_raw_blob(db))


def _store_blob(db, d: dict) -> None:
    store.upsert_account(store.init_db(str(db)), "car", KEY, json.dumps(d), SECRET)


def _telematic_calls(fake):
    return [p for _m, p, _h, _b in fake.calls if p.endswith("telematicData") or
            "telematicData?" in p]


# --- the form -----------------------------------------------------------------------

def test_form_offers_bmw_with_its_fields(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    reg = client.post("/car/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    page = client.get("/car/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "s", "code_challenge": "c" * 43, "code_challenge_method": "S256"}).text
    for needle in ('value="bmw"', 'name="car_bmw_client_id"', 'name="car_bmw_vin"',
                   "Technical access", 'data-brand="bmw"', "data-brand-picker"):
        assert needle in page


# --- sign-in ------------------------------------------------------------------------

def test_sign_in_shows_code_then_finishes_after_approval(fake_bmw, tmp_path):
    client, app = _client(fake_bmw, tmp_path)
    fake_bmw.device_state = "pending"
    r, _reg, _v = _start(client)
    assert r.status_code == 200
    assert "BMW-1234" in r.text
    assert 'href="https://customer.example/oneid/link"' in r.text
    r = _approve(client, r.text)                        # not approved yet: re-prompt
    assert r.status_code == 400
    assert "hasn&#x27;t confirmed yet" in r.text or "hasn't confirmed yet" in r.text
    assert "BMW-1234" in r.text                         # same code, new login_id
    fake_bmw.device_state = "approved"
    r = _approve(client, r.text)
    assert r.status_code == 302
    d = _blob(app)
    assert d["brand"] == "bmw" and d["vin"] == BMW_VIN and d["client_id"] == BMW_CLIENT_ID
    assert d["operations"] == [] and d["container_id"] == "C0001"
    assert d["refresh_token"] == "rt-1"
    # the container asks for our key set; PKCE rode along on the device + token calls
    assert fake_bmw.containers[0]["name"] == "missingmcp"
    assert set(fake_bmw.containers[0]["technicalDescriptors"]) == set(bmw.CONTAINER_KEYS)
    dev = next(b for _m, p, _h, b in fake_bmw.calls if p == "/device/code")
    assert b"code_challenge_method=S256" in dev
    tok = [b for _m, p, _h, b in fake_bmw.calls if p == "/token"]
    assert all(b"code_verifier=" in b for b in tok)


def test_existing_container_is_reused(fake_bmw, tmp_path):
    fake_bmw.containers += [
        {"name": "missingmcp", "purpose": "x", "containerId": "C0041", "state": "DELETED"},
        {"name": "missingmcp", "purpose": "x", "containerId": "C0042", "state": "ACTIVE"}]
    client, app = _client(fake_bmw, tmp_path)
    _connect(client, fake_bmw)
    assert _blob(app)["container_id"] == "C0042"
    assert not [1 for m, p, _h, _b in fake_bmw.calls if m == "POST" and p == "/customers/containers"]


def test_bad_client_id_format_never_reaches_bmw(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    r, _, _ = _start(client, car_bmw_client_id="not-a-uuid")
    assert r.status_code == 200 and "36 characters" in r.text
    assert fake_bmw.calls == []


def test_unknown_client_id_is_a_form_error(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    r, _, _ = _start(client, car_bmw_client_id="00000000-0000-4000-8000-000000000000")
    assert "doesn&#x27;t accept that Client ID" in r.text


def test_vin_not_on_the_account_is_rejected(fake_bmw, tmp_path):
    client, app = _client(fake_bmw, tmp_path)
    fake_bmw.vins = ["WBY00000000000099"]
    r, _, _ = _start(client)
    r = _approve(client, r.text)
    assert r.status_code == 200 and "primary user" in r.text
    assert _raw_blob(app) is None


def test_declined_code_starts_over(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    fake_bmw.device_state = "denied"
    r, _, _ = _start(client)
    r = _approve(client, r.text)
    assert "declined" in r.text and 'name="car_brand"' in r.text


# --- tools ----------------------------------------------------------------------------

def test_tools_are_read_only(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    r = client.post("/car/mcp", headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["get_vehicle_capabilities", "get_vehicle_status", "get_vehicle_location"]
    r = _call(client, token, "start_climate")
    assert r.json()["result"]["isError"] and "doesn't support" in r.text


def test_status_then_location_share_one_read(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    status = _payload(_call(client, token, "get_vehicle_status"))
    assert status["battery_percent"] == 74 and status["range_km"] == 212
    assert status["odometer_km"] == 48210
    assert status["charging"]["plug"] == "disconnected" and status["charging"]["limit_percent"] == 80
    assert status["doors"] == "closed" and status["trunk"] == "open"
    assert status["budget"]["used_last_24h_by_missingmcp"] == 1
    raw = json.dumps(status)
    assert BMW_LAT not in raw and BMW_LON not in raw and "50.08" not in raw   # no location
    loc = _payload(_call(client, token, "get_vehicle_location"))
    assert loc["lat"] == float(BMW_LAT) and loc["lon"] == float(BMW_LON)
    assert loc["cached"] is True
    assert len(_telematic_calls(fake_bmw)) == 1


def test_capabilities_make_no_upstream_call(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    before = len(fake_bmw.calls)
    caps = _payload(_call(client, token, "get_vehicle_capabilities"))
    assert caps["vehicle"] == {"brand": "bmw", "vin_tail": BMW_VIN[-6:]}
    assert len(fake_bmw.calls) == before


def test_expired_access_token_refreshes_and_persists_rotation(fake_bmw, tmp_path):
    client, app = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    d = _blob(app)
    d["access_expires_at"] = int(time.time()) + 30          # inside the refresh margin
    _store_blob(app, d)
    assert _payload(_call(client, token, "get_vehicle_status"))["battery_percent"] == 74
    stored = _blob(app)
    assert stored["refresh_token"] == "rt-2" and stored["access_token"] == "at-2"
    assert "rt-1" not in fake_bmw.valid_refresh


def test_dead_refresh_asks_for_reauth(fake_bmw, tmp_path):
    client, app = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    d = _blob(app)
    d["access_expires_at"] = 0
    _store_blob(app, d)
    fake_bmw.refresh_fails = True
    r = _call(client, token, "get_vehicle_status")
    assert r.status_code == 401 and r.json()["error"] == "invalid_token"


def test_daily_limit_is_a_tool_error(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    fake_bmw.force_api = 429
    r = _call(client, token, "get_vehicle_status")
    assert r.status_code == 200 and r.json()["result"]["isError"]
    assert "daily limit" in r.text


def test_deleted_container_asks_for_reauth(fake_bmw, tmp_path):
    client, _ = _client(fake_bmw, tmp_path)
    token = _connect(client, fake_bmw)
    fake_bmw.containers.clear()
    r = _call(client, token, "get_vehicle_status")
    assert r.status_code == 401


# --- BmwClient.ensure_fresh ----------------------------------------------------------

def test_concurrent_stale_calls_refresh_exactly_once(fake_bmw):
    cfg = _cfg(fake_bmw)
    conn = store.init_db(":memory:")
    api = bmw.BmwClient(cfg)
    first = fake_bmw._mint()
    d = {"v": 1, "brand": "bmw", "client_id": BMW_CLIENT_ID, "vin": BMW_VIN,
         "access_token": first["access_token"], "refresh_token": first["refresh_token"],
         "access_expires_at": int(time.time()) + 10, "container_id": "C0001", "operations": []}
    store.upsert_account(conn, "car", KEY, json.dumps(d), cfg.gateway_secret)

    async def both():
        return await asyncio.gather(api.ensure_fresh(conn, KEY, dict(d)),
                                    api.ensure_fresh(conn, KEY, dict(d)))
    a, b = asyncio.run(both())
    assert a["refresh_token"] == b["refresh_token"] == "rt-2"
    refreshes = [1 for _m, p, _h, body in fake_bmw.calls
                 if p == "/token" and b"grant_type=refresh_token" in body]
    assert len(refreshes) == 1
