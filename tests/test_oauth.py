import re
import hashlib
import base64
import pytest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, oauth, security
from garmin_gateway.adapters.garmin import GarminAdapter, login as garmin_login
from garmin_gateway.config import load_config

CONFIG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com"})
ADAPTER = GarminAdapter(CONFIG)


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


def test_metadata_shape():
    m = oauth.metadata(CONFIG, ADAPTER)
    assert m["issuer"] == "https://gw.example.com/garmin"
    assert m["authorization_endpoint"] == "https://gw.example.com/garmin/oauth/authorize"
    assert m["token_endpoint"] == "https://gw.example.com/garmin/oauth/token"
    assert m["registration_endpoint"] == "https://gw.example.com/garmin/oauth/register"
    assert m["code_challenge_methods_supported"] == ["S256"]


def test_protected_resource_metadata_shape():
    m = oauth.protected_resource_metadata(CONFIG, ADAPTER)
    assert m["resource"] == "https://gw.example.com/garmin/mcp"
    assert m["authorization_servers"] == ["https://gw.example.com/garmin"]


def _client_app(conn):
    async def reg(request):
        return await oauth.register_client(request, conn, ADAPTER)
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
        return await oauth.authorize_get(request, ADAPTER, state, conn, CONFIG)
    async def apost(request):
        return await oauth.authorize_post(request, ADAPTER, state, conn, CONFIG)
    app = Starlette(routes=[
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/authorize", apost, methods=["POST"]),
    ])
    return TestClient(app, follow_redirects=False), state


def _register(conn):
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude", "garmin")
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
    assert 'action="/garmin/oauth/authorize"' in r.text


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
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"), \
         patch.object(oauth, "log") as log_spy:
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
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) == '{"t":1}'
    # operators query this exact literal in Railway logs — a typo here breaks monitoring silently
    status_calls = [c.kwargs["status"] for c in log_spy.call_args_list if c.args and c.args[0] == "login-start-result"]
    assert status_calls == ["ok"]


def test_login_mfa_then_verify_redirects(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    csrf1 = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="needs_mfa", pending=("P", "S"))), \
         patch.object(oauth, "log") as log_spy:
        r1 = client.post("/oauth/authorize", data={
            "csrf": csrf1, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r1.status_code == 200 and "login_id" in r1.text
    # operators query this exact literal in Railway logs — a typo here breaks monitoring silently
    status_calls = [c.kwargs["status"] for c in log_spy.call_args_list if c.args and c.args[0] == "login-start-result"]
    assert status_calls == ["needs_mfa"]
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
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) == '{"t":9}'


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
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params, "garmin")
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
    store.create_client(conn, cid, store.hash_token(csecret), ["https://claude.ai/cb"], "Claude", "garmin")
    store.upsert_account(conn, "garmin", "me@x.cz", "{}", CONFIG.gateway_secret)
    verifier, challenge = _pkce_pair()
    code = "thecode"
    store.create_code(conn, store.hash_token(code), cid, "https://claude.ai/cb", challenge, "S256", "garmin", "me@x.cz")
    c = _token_app(conn)
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/cb",
        "client_id": cid, "client_secret": csecret, "code_verifier": verifier,
    })
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert store.account_key_for_token_hash(conn, store.hash_token(token)) == ("garmin", "me@x.cz")


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
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) is None  # not stored


def test_mfa_wrong_code_reprompts(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    params = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params, "garmin")
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "resume_login", side_effect=Exception("wrong code")):
        r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "000000"})
    assert r.status_code == 400
    assert "login_id" in r.text                          # re-prompts MFA form


def test_mfa_verify_failure_restarts(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    params = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params, "garmin")
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "resume_login", return_value='{"t":1}'), \
         patch.object(garmin_login, "verify_tokens",
                      side_effect=garmin_login.GarminLoginError("bad")):
        r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "123456"})
    assert r.status_code == 200
    assert "garmin_email" in r.text                      # back to the login form
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) is None  # not stored


def test_mfa_resume_login_error_restarts_login(conn):
    # base.py contract: resume_second_factor may raise LoginError = "start over";
    # the core must re-render the credential form, not 500
    from garmin_gateway.adapters.base import LoginError
    client, state = _authz_app(conn)
    cid = _register(conn)
    params = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params, "garmin")
    csrf = state.csrf.issue()
    with patch.object(ADAPTER, "resume_second_factor",
                      side_effect=LoginError("session vanished")):
        r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "123456"})
    assert r.status_code == 200
    assert "garmin_email" in r.text                      # back to the login form


def test_token_exchange_bad_pkce(conn):
    cid = security.new_secret(8)
    store.create_client(conn, cid, store.hash_token("s"), ["https://claude.ai/cb"], None, "garmin")
    _, challenge = _pkce_pair()
    store.create_code(conn, store.hash_token("c2"), cid, "https://claude.ai/cb", challenge, "S256", "garmin", "me@x.cz")
    c = _token_app(conn)
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": "c2", "redirect_uri": "https://claude.ai/cb",
        "client_id": cid, "client_secret": "s", "code_verifier": "WRONG",
    })
    assert r.status_code == 400


# --- Remote-adapter authorize flow: the generic core (form render, verify-then-
# persist, MFA adapter-scoping) driven through StubRemoteAdapter + fake upstream ---

import json
from conftest import StubRemoteAdapter


def _remote_authz(conn, fake_remote):
    cfg = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com"})
    adapter = StubRemoteAdapter(f"http://127.0.0.1:{fake_remote.port}/mcp")
    state = oauth.AuthState(security.CsrfStore())
    async def aget(request):
        return await oauth.authorize_get(request, adapter, state, conn, cfg)
    async def apost(request):
        return await oauth.authorize_post(request, adapter, state, conn, cfg)
    app = Starlette(routes=[
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/authorize", apost, methods=["POST"]),
    ])
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude", "acme")
    return TestClient(app, follow_redirects=False), state, cfg, cid


def test_remote_authorize_get_renders_form(conn, fake_remote):
    client, _, _, cid = _remote_authz(conn, fake_remote)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "response_type": "code",
    })
    assert r.status_code == 200
    assert 'action="/acme/oauth/authorize"' in r.text
    assert "{OPERATOR_NAME}" not in r.text and "{OAUTH_FIELDS}" not in r.text  # placeholders filled
    assert 'name="csrf"' in r.text                       # hidden OAuth fields injected


def test_remote_login_verifies_upstream_and_redirects(conn, fake_remote):
    client, state, cfg, cid = _remote_authz(conn, fake_remote)
    csrf = state.csrf.issue()
    r = client.post("/oauth/authorize", data={
        "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "acme_user": "Me@X.cz", "acme_pass": "pw",
    })
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"] and q["code"]
    # verify() hit the upstream with the injected credential headers
    _, path, hdrs, _ = fake_remote.calls[-1]
    assert path == "/mcp" and hdrs.get("x-acme-user") == "Me@X.cz" and hdrs.get("x-acme-pass") == "pw"
    # blob persisted under the normalized key, credentials intact
    blob = store.get_account_tokens(conn, "acme", "me@x.cz", cfg.gateway_secret)
    assert json.loads(blob) == {"user": "Me@X.cz", "pass": "pw"}


def test_remote_verify_failure_rerenders_form_and_persists_nothing(conn, fake_remote):
    client, state, cfg, cid = _remote_authz(conn, fake_remote)
    fake_remote.response_status = 401                    # upstream rejects the credentials
    csrf = state.csrf.issue()
    r = client.post("/oauth/authorize", data={
        "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "acme_user": "me@x.cz", "acme_pass": "wrong",
    })
    assert r.status_code == 200
    assert 'name="csrf"' in r.text                       # back to the login form
    assert "check your credentials" in r.text
    assert store.get_account_tokens(conn, "acme", "me@x.cz", cfg.gateway_secret) is None


def test_remote_invalid_login_input_rerenders_form(conn, fake_remote):
    client, state, cfg, cid = _remote_authz(conn, fake_remote)
    csrf = state.csrf.issue()
    r = client.post("/oauth/authorize", data={
        "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "acme_user": "not-an-email", "acme_pass": "pw",
    })
    assert r.status_code == 200
    assert "Please enter a valid email address." in r.text
    assert not fake_remote.calls                         # rejected before touching the upstream


def test_mfa_login_id_from_another_adapter_is_rejected(conn, fake_remote):
    # AuthState is shared across adapter routes: a login_id minted mid-Garmin-MFA
    # must read as expired on another adapter's authorize endpoint, not crash the flow
    client, state, _, _ = _remote_authz(conn, fake_remote)
    params = {"client_id": "cg", "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params, "garmin")
    csrf = state.csrf.issue()
    r = client.post("/oauth/authorize", data={"csrf": csrf, "login_id": lid, "mfa_code": "123456"})
    assert r.status_code == 400
    assert "MFA session expired" in r.text
