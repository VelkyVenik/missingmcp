"""End-to-end: the whoop adapter through build_app — discovery, upstream-OAuth
authorize → callback, downstream token exchange, and MCP tool calls, all
against the fake WHOOP upstream."""
import base64
import hashlib
from urllib.parse import urlparse, parse_qs
from starlette.testclient import TestClient
from missingmcp.app import build_app
from missingmcp.config import load_config

BASE_ENV = {"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
            "DB_PATH": ":memory:", "DATA_DIR": "/tmp"}


def _client(fake):
    cfg = load_config({**BASE_ENV,
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                       "WHOOP_API_BASE": f"http://127.0.0.1:{fake.port}"})
    return TestClient(build_app(cfg)), cfg


def _pkce():
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _connect(client):
    """Run the whole flow; returns (bearer_token, registration)."""
    reg = client.post("/whoop/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    verifier, challenge = _pkce()
    r = client.get("/whoop/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "claude-state", "code_challenge": challenge,
        "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 302
    up = urlparse(r.headers["location"])
    upq = parse_qs(up.query)
    r = client.get("/whoop/oauth/callback",
                   params={"code": "upstream-code", "state": upq["state"][0]},
                   follow_redirects=False)
    assert r.status_code == 302
    cbq = parse_qs(urlparse(r.headers["location"]).query)
    assert cbq["state"] == ["claude-state"]
    r = client.post("/whoop/oauth/token", data={
        "grant_type": "authorization_code", "code": cbq["code"][0],
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert r.status_code == 200
    return r.json()["access_token"], reg


def test_discovery_documents(fake_whoop):
    client, _cfg = _client(fake_whoop)
    r = client.get("/.well-known/oauth-authorization-server/whoop")
    assert r.status_code == 200
    assert r.json()["issuer"] == "https://gw.example.com/whoop"
    r = client.get("/.well-known/oauth-protected-resource/whoop/mcp")
    assert r.json()["resource"] == "https://gw.example.com/whoop/mcp"


def test_authorize_redirect_carries_whoop_params(fake_whoop):
    client, _cfg = _client(fake_whoop)
    reg = client.post("/whoop/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    _verifier, challenge = _pkce()
    r = client.get("/whoop/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "cl", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False)
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert loc.startswith(f"http://127.0.0.1:{fake_whoop.port}/oauth/oauth2/auth")
    assert q["client_id"] == ["cid-1"] and len(q["state"][0]) >= 8
    assert q["redirect_uri"] == ["https://gw.example.com/whoop/oauth/callback"]
    assert "offline" in q["scope"][0] and "read:recovery" in q["scope"][0]


def test_full_connect_flow_and_tool_call(fake_whoop):
    client, cfg = _client(fake_whoop)
    token, _reg = _connect(client)
    r = client.post("/whoop/mcp", headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "get_profile", "arguments": {}}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    assert "User@Example.com" in result["content"][0]["text"]


def test_reconnect_same_account_succeeds(fake_whoop):
    client, cfg = _client(fake_whoop)
    _connect(client)
    r = client.get("/whoop")           # landing renders → the app itself is the oracle:
    assert r.status_code == 200
    # the persisted row is observable through a second connect: same account, no dup
    token2, _ = _connect(client)
    assert token2


def test_authorize_post_is_not_registered_for_whoop(fake_whoop):
    client, _cfg = _client(fake_whoop)
    r = client.post("/whoop/oauth/authorize", data={"anything": "x"})
    assert r.status_code == 405        # GET-only route: no credential form to POST


def test_stale_refresh_maps_to_reauth_401(fake_whoop):
    client, _cfg = _client(fake_whoop)
    token, _reg = _connect(client)
    fake_whoop.refresh_fails = True
    fake_whoop.valid_tokens.clear()    # current access token stops working upstream
    r = client.post("/whoop/mcp", headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "get_cycles", "arguments": {}}})
    # end-to-end (full middleware): dead refresh → 401 + RFC 9728 challenge, so
    # Claude re-runs authorization instead of retrying a dead-end 502.
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"
    assert 'resource_metadata="https://gw.example.com/.well-known/oauth-protected-resource/whoop/mcp"' \
        in r.headers["www-authenticate"]


def test_without_credentials_whoop_is_absent():
    cfg = load_config(BASE_ENV)        # no WHOOP_CLIENT_ID/SECRET
    client = TestClient(build_app(cfg))
    assert client.get("/.well-known/oauth-authorization-server/whoop").status_code == 404
    assert client.get("/whoop", follow_redirects=False).status_code == 404
    # garmin is untouched either way
    assert client.get("/.well-known/oauth-authorization-server/garmin").status_code == 200
