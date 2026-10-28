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


def _connect(client):
    page, reg, verifier = _form_page(client)
    r = _submit(client, page)
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
                   'name="car_api_key"', 'name="car_vin"', "Chytrá domácnost"):
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


def test_landing_renders(fake_skoda):
    r = _client(fake_skoda).get("/car")
    assert r.status_code == 200
