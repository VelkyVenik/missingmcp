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
