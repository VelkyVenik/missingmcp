import json
import os
import pytest
from unittest.mock import patch, MagicMock
from garmin_gateway.adapters.garmin import login as garmin_login


def _fake_garmin_factory(needs_mfa=False, dump_payload='{"oauth":"tok"}'):
    """Return a fake Garmin class whose .dump writes garmin_tokens.json."""
    def dump(path):
        with open(os.path.join(path, "garmin_tokens.json"), "w") as f:
            f.write(dump_payload)

    def make(*args, **kwargs):
        g = MagicMock()
        g.client.dump.side_effect = dump
        if needs_mfa and (kwargs.get("password") or len(args) >= 2):
            g.login.return_value = ("needs_mfa", "STATE")
        else:
            g.login.return_value = (None, None)
        g.get_full_name.return_value = "Vaclav S"
        return g
    return make


def test_login_no_mfa_returns_tokens():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        r = garmin_login.start_login("me@x.cz", "pw")
    assert r.status == "ok"
    assert json.loads(r.tokens_json) == {"oauth": "tok"}


def test_login_needs_mfa_then_resume():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory(needs_mfa=True)):
        r = garmin_login.start_login("me@x.cz", "pw")
        assert r.status == "needs_mfa"
        assert r.tokens_json is None
        tokens = garmin_login.resume_login(r.pending, "123456")
    assert json.loads(tokens) == {"oauth": "tok"}


def test_login_retries_blocked_then_succeeds():
    calls = {"n": 0}

    def dump(path):
        with open(os.path.join(path, "garmin_tokens.json"), "w") as f:
            f.write('{"oauth":"tok"}')

    def make(*a, **k):
        g = MagicMock()
        g.client.dump.side_effect = dump

        def login(*la, **lk):
            calls["n"] += 1
            if calls["n"] == 1:
                raise garmin_login.GarminConnectConnectionError("Portal login failed: HTTP 403")
            return (None, None)

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        r = garmin_login.start_login("me@x.cz", "pw", attempts=2, backoff=0, sleep=lambda s: None)
    assert r.status == "ok" and calls["n"] == 2      # retried once, then succeeded


def test_login_auth_error_not_retried():
    calls = {"n": 0}

    def make(*a, **k):
        g = MagicMock()

        def login(*la, **lk):
            calls["n"] += 1
            raise garmin_login.GarminConnectAuthenticationError("401 Unauthorized")

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "wrong", attempts=3, sleep=lambda s: None)
    assert ei.value.reason == "auth" and calls["n"] == 1   # wrong password: never retried


def test_login_blocked_exhausted_raises_blocked():
    def make(*a, **k):
        g = MagicMock()
        g.login.side_effect = garmin_login.GarminConnectTooManyRequestsError("429 rate limited")
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "pw", attempts=2, backoff=0, sleep=lambda s: None)
    assert ei.value.reason == "blocked"


def test_verify_tokens_returns_name():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        name = garmin_login.verify_tokens('{"oauth":"tok"}')
    assert name == "Vaclav S"


def test_verify_tokens_raises_when_no_profile():
    def make(*a, **k):
        g = MagicMock()
        g.get_full_name.return_value = None
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError):
            garmin_login.verify_tokens('{"oauth":"tok"}')
