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
