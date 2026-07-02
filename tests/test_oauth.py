import re
import hashlib
import base64
import pytest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, oauth, security, garmin_login
from garmin_gateway.config import load_config

CONFIG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com"})


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


def test_metadata_shape():
    m = oauth.metadata(CONFIG)
    assert m["issuer"] == "https://gw.example.com"
    assert m["authorization_endpoint"] == "https://gw.example.com/oauth/authorize"
    assert m["token_endpoint"] == "https://gw.example.com/oauth/token"
    assert m["registration_endpoint"] == "https://gw.example.com/oauth/register"
    assert m["code_challenge_methods_supported"] == ["S256"]


def _client_app(conn):
    async def reg(request):
        return await oauth.register_client(request, conn)
    return TestClient(Starlette(routes=[Route("/oauth/register", reg, methods=["POST"])]))


def test_register_returns_client_id(conn):
    c = _client_app(conn)
    resp = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"]
    assert body["client_secret"]
    assert body["redirect_uris"] == ["https://claude.ai/cb"]
    stored = store.get_client(conn, body["client_id"])
    assert stored is not None
    assert stored["client_secret_hash"] == store.hash_token(body["client_secret"])


def test_register_rejects_missing_redirect_uris(conn):
    c = _client_app(conn)
    resp = c.post("/oauth/register", json={})
    assert resp.status_code == 400


def test_register_rejects_empty_redirect_uris(conn):
    c = _client_app(conn)
    assert c.post("/oauth/register", json={"redirect_uris": []}).status_code == 400


def test_register_rejects_empty_string_redirect_uri(conn):
    c = _client_app(conn)
    assert c.post("/oauth/register", json={"redirect_uris": [""]}).status_code == 400


def _authz_app(conn):
    state = oauth.AuthState(security.CsrfStore())
    async def aget(request):
        return await oauth.authorize_get(request, None, state, conn, CONFIG)
    async def apost(request):
        return await oauth.authorize_post(request, None, state, conn, CONFIG)
    app = Starlette(routes=[
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/authorize", apost, methods=["POST"]),
    ])
    return TestClient(app, follow_redirects=False), state


def _register(conn):
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude")
    return cid


def test_authorize_get_renders_form(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "response_type": "code",
    })
    assert r.status_code == 200
    assert "garmin_email" in r.text
    assert "csrf" in r.text


def test_authorize_get_rejects_bad_redirect(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://evil.com/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
    })
    assert r.status_code == 400


def test_login_no_mfa_redirects_with_code(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    # obtain a CSRF token the way the GET would mint one
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="ok", tokens_json='{"t":1}')), \
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"):
        r = client.post("/oauth/authorize", data={
            "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "Me@X.cz", "garmin_password": "pw",
        })
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    assert q["code"]
    # account stored under normalized (lowercased) email
    assert store.get_account_tokens(conn, "me@x.cz", CONFIG.gateway_secret) == '{"t":1}'


def test_login_mfa_then_verify_redirects(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    csrf1 = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        r1 = client.post("/oauth/authorize", data={
            "csrf": csrf1, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r1.status_code == 200 and "login_id" in r1.text
    assert "{OPERATOR_NAME}" not in r1.text  # operator placeholder must be filled on the normal MFA page
    # extract login_id and a fresh csrf rendered into the MFA page
    login_id = re.search(r'name="login_id" value="([^"]+)"', r1.text).group(1)
    csrf2 = re.search(r'name="csrf" value="([^"]+)"', r1.text).group(1)
    with patch.object(garmin_login, "resume_login", return_value='{"t":9}'), \
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"):
        r2 = client.post("/oauth/authorize", data={
            "csrf": csrf2, "login_id": login_id, "mfa_code": "123456",
        })
    assert r2.status_code == 302
    assert store.get_account_tokens(conn, "me@x.cz", CONFIG.gateway_secret) == '{"t":9}'


def test_authorize_post_rejects_bad_csrf(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.post("/oauth/authorize", data={
        "csrf": "forged", "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "garmin_email": "me@x.cz", "garmin_password": "pw",
    })
    assert r.status_code == 400


def test_authorize_get_rejects_non_s256(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "plain",
    })
    assert r.status_code == 400


def test_authorize_get_escapes_reflected_state(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": '"><script>alert(1)</script>', "code_challenge": "abc",
        "code_challenge_method": "S256",
    })
    assert r.status_code == 200
    assert "<script>" not in r.text  # reflected state must be escaped


def test_authorize_post_mfa_rejects_tampered_redirect(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    params = {"client_id": cid, "redirect_uri": "https://evil.com/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256", "_email": "me@x.cz"}
    lid = state.put_mfa(("P", "S"), params)
    csrf = state.csrf.issue()
    r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "123456"})
    assert r.status_code == 400


def _token_app(conn):
    async def tok(request):
        return await oauth.token_exchange(request, conn, CONFIG)
    return TestClient(Starlette(routes=[Route("/oauth/token", tok, methods=["POST"])]))


def _pkce_pair():
    verifier = "verifier-abcdef-1234567890"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def test_token_exchange_happy_path(conn):
    cid = security.new_secret(8)
    csecret = "topsecret"
    store.create_client(conn, cid, store.hash_token(csecret), ["https://claude.ai/cb"], "Claude")
    store.upsert_account(conn, "me@x.cz", "{}", CONFIG.gateway_secret)
    verifier, challenge = _pkce_pair()
    code = "thecode"
    store.create_code(conn, store.hash_token(code), cid, "https://claude.ai/cb", challenge, "S256", "me@x.cz")
    c = _token_app(conn)
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/cb",
        "client_id": cid, "client_secret": csecret, "code_verifier": verifier,
    })
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert store.account_key_for_token_hash(conn, store.hash_token(token)) == "me@x.cz"


def test_login_blocked_shows_retry_message(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      side_effect=garmin_login.GarminLoginError("429 rate limited", reason="blocked")):
        r = client.post("/oauth/authorize", data={
            "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r.status_code == 200
    assert "rate-limiting" in r.text                         # Garmin-side limit, not "wrong password"
    assert "not your password" in r.text
    assert "garmin_email" in r.text                          # form re-rendered to retry


def test_login_verify_failure_rerenders_form(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="ok", tokens_json='{"t":1}')), \
         patch.object(garmin_login, "verify_tokens",
                      side_effect=garmin_login.GarminLoginError("bad")):
        r = client.post("/oauth/authorize", data={
            "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r.status_code == 200
    assert "garmin_email" in r.text                      # re-rendered login form
    assert store.get_account_tokens(conn, "me@x.cz", CONFIG.gateway_secret) is None  # not stored


def test_mfa_wrong_code_reprompts(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    params = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256", "_email": "me@x.cz"}
    lid = state.put_mfa(("P", "S"), params)
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "resume_login", side_effect=Exception("wrong code")):
        r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "000000"})
    assert r.status_code == 400
    assert "login_id" in r.text                          # re-prompts MFA form


def test_mfa_verify_failure_restarts(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    params = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256", "_email": "me@x.cz"}
    lid = state.put_mfa(("P", "S"), params)
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "resume_login", return_value='{"t":1}'), \
         patch.object(garmin_login, "verify_tokens",
                      side_effect=garmin_login.GarminLoginError("bad")):
        r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "123456"})
    assert r.status_code == 200
    assert "garmin_email" in r.text                      # back to the login form


def test_token_exchange_bad_pkce(conn):
    cid = security.new_secret(8)
    store.create_client(conn, cid, store.hash_token("s"), ["https://claude.ai/cb"], None)
    _, challenge = _pkce_pair()
    store.create_code(conn, store.hash_token("c2"), cid, "https://claude.ai/cb", challenge, "S256", "me@x.cz")
    c = _token_app(conn)
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": "c2", "redirect_uri": "https://claude.ai/cb",
        "client_id": cid, "client_secret": "s", "code_verifier": "WRONG",
    })
    assert r.status_code == 400
