"""End-to-end: the car adapter through build_app — discovery, the brand/key/VIN
authorize form, downstream token exchange and MCP tool calls, all against the
fake Škoda upstream."""
import base64
import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urlparse
import pytest
from starlette.testclient import TestClient
from conftest import SKODA_KEY, SKODA_VIN
from missingmcp.app import build_app
from missingmcp.config import load_config

BASE_ENV = {"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
            "DB_PATH": ":memory:", "DATA_DIR": "/tmp"}
EMAIL = "Driver@Example.com"


def _client(fake):
    cfg = load_config({**BASE_ENV, "CAR_SKODA_API_BASE": f"http://127.0.0.1:{fake.port}"})
    return TestClient(build_app(cfg))


def _pkce():
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _form_page(client):
    reg = client.post("/car/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    verifier, challenge = _pkce()
    r = client.get("/car/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "claude-state", "code_challenge": challenge,
        "code_challenge_method": "S256"})
    assert r.status_code == 200
    return r.text, reg, verifier


def _hidden(page: str) -> dict:
    return {n: html.unescape(v) for n, v in
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)}


def _submit(client, page, **fields):
    data = {**_hidden(page), "car_brand": "skoda", "car_email": EMAIL,
            "car_api_key": SKODA_KEY, "car_vin": SKODA_VIN, **fields}
    return client.post("/car/oauth/authorize", data=data, follow_redirects=False)


def _connect(client, **fields):
    page, reg, verifier = _form_page(client)
    r = _submit(client, page, **fields)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["claude-state"]
    r = client.post("/car/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert r.status_code == 200
    return r.json()["access_token"]


def _mcp(client, token, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/car/mcp", headers={"Authorization": f"Bearer {token}"}, json=body)


def test_discovery_documents(fake_skoda):
    client = _client(fake_skoda)
    r = client.get("/.well-known/oauth-authorization-server/car")
    assert r.status_code == 200 and r.json()["issuer"] == "https://gw.example.com/car"
    r = client.get("/.well-known/oauth-protected-resource/car/mcp")
    assert r.json()["resource"] == "https://gw.example.com/car/mcp"


def test_form_renders_brand_and_fields(fake_skoda):
    page, _, _ = _form_page(_client(fake_skoda))
    for needle in ('name="car_brand"', 'value="skoda"', 'name="car_email"',
                   'name="car_api_key"', 'name="car_vin"', "Smart home"):
        assert needle in page


def test_full_connect_flow_and_tools(fake_skoda):
    client = _client(fake_skoda)
    token = _connect(client)
    # sign-in cost exactly one upstream call: the operations probe
    assert [p for _, p, _, _ in fake_skoda.calls] == \
        [f"/api/v1/vehicles/{SKODA_VIN}?include=operations"]
    tools = {t["name"] for t in _mcp(client, token, "tools/list").json()["result"]["tools"]}
    assert "start_climate" in tools and "start_ventilation" not in tools
    r = _mcp(client, token, "tools/call", {"name": "get_vehicle_status", "arguments": {}})
    res = r.json()["result"]
    assert res["isError"] is False
    assert json.loads(res["content"][0]["text"])["battery_percent"] == 75


@pytest.mark.parametrize("fields,force,needle", [
    ({"car_api_key": "wrong"}, None, "isn&#x27;t valid"),
    ({"car_vin": "TMBTEST0000000009"}, None, "doesn&#x27;t cover"),
    ({}, (429, "rate-limit-exceeded"), "20 min"),
    ({}, (500, "internal"), "didn&#x27;t answer"),
])
def test_form_errors_rerender(fake_skoda, fields, force, needle):
    client = _client(fake_skoda)
    page, _, _ = _form_page(client)
    fake_skoda.force = force
    r = _submit(client, page, **fields)
    assert r.status_code == 200
    assert needle in r.text or html.unescape(needle) in r.text
    assert 'name="car_vin"' in r.text            # back on the form


@pytest.mark.parametrize("fields", [
    {"car_email": "not-an-email"}, {"car_api_key": "  "}, {"car_vin": "SHORT"},
    {"car_brand": "tesla"},
])
def test_invalid_form_makes_no_upstream_call(fake_skoda, fields):
    client = _client(fake_skoda)
    page, _, _ = _form_page(client)
    r = _submit(client, page, **fields)
    assert r.status_code == 200 and 'name="car_vin"' in r.text
    assert fake_skoda.calls == []


def test_key_and_vin_never_logged(fake_skoda, capsys):
    client = _client(fake_skoda)
    token = _connect(client)
    _mcp(client, token, "tools/call", {"name": "get_vehicle_status", "arguments": {}})
    fake_skoda.force = (504, "gateway-timeout")
    _mcp(client, token, "tools/call", {"name": "start_climate", "arguments": {}})
    out = capsys.readouterr()
    for secret in (SKODA_KEY, SKODA_VIN):
        assert secret not in out.out and secret not in out.err


ATTACKER_KEY, ATTACKER_VIN = "key-attacker", "TMBTEST0000000002"


def _status_call(client, token):
    return _mcp(client, token, "tools/call", {"name": "get_vehicle_capabilities",
                                              "arguments": {}})


def test_unverified_email_squat_cannot_reach_the_victims_car(fake_skoda):
    """Codex P1: an attacker connects their own car under the victim's email; the
    victim later connects theirs. The attacker's token must die then, never
    inherit the victim's key."""
    fake_skoda.keys[ATTACKER_KEY] = {ATTACKER_VIN}
    client = _client(fake_skoda)
    attacker_token = _connect(client, car_api_key=ATTACKER_KEY, car_vin=ATTACKER_VIN)
    assert _status_call(client, attacker_token).status_code == 200
    victim_token = _connect(client)                      # same email, the victim's own car
    assert _status_call(client, attacker_token).status_code == 401
    r = _status_call(client, victim_token)
    assert json.loads(r.json()["result"]["content"][0]["text"])["vehicle"]["vin_tail"] \
        == SKODA_VIN[-6:]
    # attacker re-squats: the victim is logged out, but the attacker sees only their own car
    attacker_token2 = _connect(client, car_api_key=ATTACKER_KEY, car_vin=ATTACKER_VIN)
    assert _status_call(client, victim_token).status_code == 401
    r = _status_call(client, attacker_token2)
    assert json.loads(r.json()["result"]["content"][0]["text"])["vehicle"]["vin_tail"] \
        == ATTACKER_VIN[-6:]


def test_pending_code_dies_when_the_car_changes(fake_skoda):
    fake_skoda.keys[ATTACKER_KEY] = {ATTACKER_VIN}
    client = _client(fake_skoda)
    page, reg, verifier = _form_page(client)
    r = _submit(client, page, car_api_key=ATTACKER_KEY, car_vin=ATTACKER_VIN)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    _connect(client)                                     # victim connects before the exchange
    r = client.post("/car/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert r.status_code == 400


def test_reconnecting_the_same_car_keeps_other_devices_signed_in(fake_skoda):
    fake_skoda.keys["key-2"] = {SKODA_VIN}
    client = _client(fake_skoda)
    phone = _connect(client)
    laptop = _connect(client, car_api_key="key-2")       # fresh key, same car
    assert _status_call(client, phone).status_code == 200
    assert _status_call(client, laptop).status_code == 200


def test_expired_key_on_tool_call_is_reauth_401(fake_skoda):
    client = _client(fake_skoda)
    token = _connect(client)
    fake_skoda.force = (401, "api-key-expired")
    r = _mcp(client, token, "tools/call", {"name": "get_vehicle_status", "arguments": {}})
    assert r.status_code == 401
    assert 'resource_metadata="https://gw.example.com/.well-known/oauth-protected-resource/car/mcp"' \
        in r.headers["www-authenticate"]


def test_get_and_delete_are_405(fake_skoda):
    client = _client(fake_skoda)
    token = _connect(client)
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/car/mcp", headers=h).status_code == 405
    assert client.delete("/car/mcp", headers=h).status_code == 405


def test_landing_lists_every_tool_brand_and_client(fake_skoda):
    from missingmcp.adapters.car.mcp import TOOLS
    r = _client(fake_skoda).get("/car")
    assert r.status_code == 200
    for name, *_ in TOOLS:
        assert f"<code>{name}</code>" in r.text
    for needle in ("Škoda", "Claude", "ChatGPT", "Grok", "https://gw.example.com/car/mcp",
                   "Beta"):
        assert needle in r.text
    assert "{USAGE_METER_CAR}" not in r.text      # placeholder filled per request


def test_form_uses_no_data_uris_blocked_by_csp(fake_skoda):
    """PR #66 review: CSP default-src 'self' blocks data: images, so the styled
    select's chevron must not be one."""
    client = _client(fake_skoda)
    reg = client.post("/car/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    _, challenge = _pkce()
    r = client.get("/car/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb", "state": "s",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    csp = r.headers["content-security-policy"]
    assert "data:" not in csp
    assert "url(\"data:" not in r.text and "url('data:" not in r.text and "url(data:" not in r.text


def test_myskoda_screenshots_served_and_linked(fake_skoda):
    client = _client(fake_skoda)
    r = client.get("/static/myskoda-smart-home.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    page, _, _ = _form_page(client)
    assert "/static/myskoda-smart-home.png" in page
    assert "/static/myskoda-smart-home.png" in client.get("/car").text


def test_home_page_does_not_promote_car(fake_skoda):
    assert "/car" not in _client(fake_skoda).get("/").text
