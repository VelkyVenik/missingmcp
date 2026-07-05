import dataclasses
import os
import stat
import pytest
from garmin_gateway.adapters import base
from garmin_gateway.config import load_config
from garmin_gateway.adapters.garmin import GarminWorkerForward


def test_login_ok_is_frozen():
    r = base.LoginOk(account_key="me@x.cz", blob='{"t":1}')
    assert r.account_key == "me@x.cz" and r.blob == '{"t":1}'
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.account_key = "other"


def test_login_error_carries_reason():
    e = base.LoginError("try later", reason="blocked")
    assert str(e) == "try later" and e.reason == "blocked"
    assert base.LoginError("x").reason == "unknown"      # default


def test_second_factor_error_carries_state():
    state = ("pending", "me@x.cz")
    e = base.SecondFactorError("wrong code", state=state)
    assert str(e) == "wrong code" and e.state is state


CFG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://x",
                   "GARMIN_MCP_CMD": "uvx garmin-mcp"})


def test_garmin_forward_command_comes_from_config():
    assert GarminWorkerForward(CFG).command() == ["uvx", "garmin-mcp"]


def test_garmin_forward_env_is_the_documented_contract():
    env = GarminWorkerForward(CFG).env(9007, "/data/users/me/tokens")
    assert env == {
        "GARMIN_MCP_TRANSPORT": "streamable-http",
        "GARMIN_MCP_HOST": "127.0.0.1",
        "GARMIN_MCP_PORT": "9007",
        "GARMINTOKENS": "/data/users/me/tokens",
    }


def test_garmin_forward_materialize_writes_0600_tokens_file(tmp_path):
    GarminWorkerForward(CFG).materialize('{"t":1}', str(tmp_path))
    path = tmp_path / "garmin_tokens.json"
    assert path.read_text() == '{"t":1}'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


from unittest.mock import patch
from garmin_gateway.adapters import base
from garmin_gateway.adapters.garmin import GarminAdapter, login


def _adapter():
    return GarminAdapter(CFG)


def test_adapter_attrs():
    a = _adapter()
    assert a.name == "garmin" and a.display_name == "Garmin"
    assert a.authorize_template == "authorize.html"
    assert a.second_factor_template == "mfa.html"
    assert a.forward.command() == ["uvx", "garmin-mcp"]
    assert a.login_hint({"garmin_email": "Me@X.cz"}) == "Me@X.cz"


def test_start_login_ok_normalizes_account_key():
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json='{"t":1}')):
        r = _adapter().start_login({"garmin_email": " Me@X.cz ", "garmin_password": "pw"})
    assert isinstance(r, base.LoginOk)
    assert r.account_key == "me@x.cz" and r.blob == '{"t":1}'


def test_start_login_mfa_state_carries_email():
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        r = _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert isinstance(r, base.SecondFactorNeeded)
    assert r.state == (("P", "S"), "me@x.cz")


def test_start_login_blocked_maps_message_and_reason():
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("429", reason="blocked")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert ei.value.reason == "blocked"
    assert "rate-limiting" in str(ei.value) and "not your password" in str(ei.value)


def test_start_login_auth_error_maps_message():
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("bad", reason="auth")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert ei.value.reason == "auth" and "check your Garmin email" in str(ei.value)


def test_resume_ok_returns_login_ok():
    with patch.object(login, "resume_login", return_value='{"t":9}'):
        r = _adapter().resume_second_factor((("P", "S"), "Me@X.cz"), {"mfa_code": "123456"})
    assert r == base.LoginOk(account_key="me@x.cz", blob='{"t":9}')


def test_resume_failure_is_retryable_with_same_state():
    state = (("P", "S"), "me@x.cz")
    with patch.object(login, "resume_login", side_effect=Exception("wrong code")):
        with pytest.raises(base.SecondFactorError) as ei:
            _adapter().resume_second_factor(state, {"mfa_code": "000000"})
    assert ei.value.state is state
    assert "Incorrect or expired code" in str(ei.value)


def test_verify_ok_and_failure():
    with patch.object(login, "verify_tokens", return_value="Vaclav S"):
        assert _adapter().verify('{"t":1}') == "Vaclav S"
    with patch.object(login, "verify_tokens", side_effect=login.GarminLoginError("bad")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().verify('{"t":1}')
    assert "could not be verified" in str(ei.value)


from garmin_gateway.adapters import build_adapters


def test_registry_builds_all_adapters():
    adapters = build_adapters(CFG)
    assert set(adapters) == {"garmin", "rohlik"}
    assert adapters["garmin"].name == "garmin"
    assert adapters["garmin"].forward.command() == ["uvx", "garmin-mcp"]
    assert adapters["rohlik"].name == "rohlik"
    assert adapters["rohlik"].forward.upstream_url == "https://mcp.rohlik.cz/mcp"


# --- Rohlik adapter (remote-forward strategy A) ---

import json
from conftest import _free_port
from garmin_gateway.adapters.rohlik import RohlikAdapter


def _rohlik(url="https://mcp.rohlik.cz/mcp"):
    return RohlikAdapter(load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://x",
                                      "ROHLIK_MCP_URL": url}))


def test_rohlik_adapter_attrs():
    a = _rohlik()
    assert a.name == "rohlik" and a.display_name == "Rohlík"
    assert a.authorize_template == "rohlik_authorize.html"
    assert a.second_factor_template == ""
    assert a.landing_template == "rohlik.html"
    assert a.forward.upstream_url == "https://mcp.rohlik.cz/mcp"
    assert a.login_hint({"rohlik_email": "Me@X.cz"}) == "Me@X.cz"


def test_rohlik_forward_headers_are_the_ts_proxy_contract():
    blob = json.dumps({"email": "Me@X.cz", "password": "tajné heslo"})
    hdrs = _rohlik().forward.headers(blob)
    assert hdrs == {"rhl-email": b"Me@X.cz", "rhl-pass": "tajné heslo".encode("latin-1")}


def test_rohlik_start_login_ok_normalizes_key_and_keeps_creds_in_blob():
    r = _rohlik().start_login({"rohlik_email": " Me@X.cz ", "rohlik_password": "pw 1"})
    assert isinstance(r, base.LoginOk)
    assert r.account_key == "me@x.cz"
    assert json.loads(r.blob) == {"email": "Me@X.cz", "password": "pw 1"}


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "a b@c.cz", "a@" + "b" * 250 + ".cz"])
def test_rohlik_start_login_rejects_bad_email(email):
    with pytest.raises(base.LoginError) as ei:
        _rohlik().start_login({"rohlik_email": email, "rohlik_password": "pw"})
    assert ei.value.reason == "auth" and "valid email" in str(ei.value)


@pytest.mark.parametrize("password", ["", "x" * 1001])
def test_rohlik_start_login_rejects_bad_password(password):
    with pytest.raises(base.LoginError) as ei:
        _rohlik().start_login({"rohlik_email": "me@x.cz", "rohlik_password": password})
    assert ei.value.reason == "auth" and "your password" in str(ei.value)


def test_rohlik_start_login_rejects_unforwardable_characters():
    with pytest.raises(base.LoginError) as ei:  # č is outside latin-1 → can't go in a header
        _rohlik().start_login({"rohlik_email": "me@x.cz", "rohlik_password": "hesíčko"})
    assert ei.value.reason == "auth" and "cannot be sent" in str(ei.value)


def test_rohlik_resume_second_factor_is_unreachable():
    with pytest.raises(base.LoginError):
        _rohlik().resume_second_factor(None, {})


def _rohlik_blob():
    return json.dumps({"email": "me@x.cz", "password": "pw"})


def test_rohlik_verify_ok_injects_headers(fake_remote):
    a = _rohlik(f"http://127.0.0.1:{fake_remote.port}/mcp")
    assert a.verify(_rohlik_blob()) == "me@x.cz"
    _, path, hdrs, body = fake_remote.calls[-1]
    assert path == "/mcp"
    assert hdrs.get("rhl-email") == "me@x.cz" and hdrs.get("rhl-pass") == "pw"
    assert hdrs.get("Accept") == "application/json, text/event-stream"
    assert json.loads(body)["method"] == "initialize"


@pytest.mark.parametrize("status", [401, 403])
def test_rohlik_verify_auth_failure(fake_remote, status):
    fake_remote.response_status = status
    a = _rohlik(f"http://127.0.0.1:{fake_remote.port}/mcp")
    with pytest.raises(base.LoginError) as ei:
        a.verify(_rohlik_blob())
    assert ei.value.reason == "auth" and "check your Rohlík email and password" in str(ei.value)


def test_rohlik_verify_upstream_5xx_is_unreachable(fake_remote):
    fake_remote.response_status = 503
    a = _rohlik(f"http://127.0.0.1:{fake_remote.port}/mcp")
    with pytest.raises(base.LoginError) as ei:
        a.verify(_rohlik_blob())
    assert ei.value.reason == "unknown" and "could not be reached" in str(ei.value)


def test_rohlik_verify_transport_error_is_unreachable():
    a = _rohlik(f"http://127.0.0.1:{_free_port()}/mcp")   # nothing listens on a freed port
    with pytest.raises(base.LoginError) as ei:
        a.verify(_rohlik_blob())
    assert ei.value.reason == "unknown" and "could not be reached" in str(ei.value)
