import json
import os
import pytest
from unittest.mock import patch, MagicMock
from missingmcp.adapters.garmin import login as garmin_login


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


def test_login_does_not_retry_blocked():
    # Ticket 12: a second round after "blocked" only doubles our SSO footprint
    # on an egress Cloudflare is scoring; the adapter's cooldowns decide next.
    calls = {"n": 0}

    def make(*a, **k):
        g = MagicMock()

        def login(*la, **lk):
            calls["n"] += 1
            raise garmin_login.GarminConnectConnectionError("Portal login failed: HTTP 403")

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "pw", attempts=2, backoff=0, sleep=lambda s: None)
    assert ei.value.reason == "blocked" and calls["n"] == 1


def test_login_retries_unexpected_failure_once():
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
                raise ValueError("flaky parse")
            return (None, None)

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        r = garmin_login.start_login("me@x.cz", "pw", attempts=2, backoff=0, sleep=lambda s: None)
    assert r.status == "ok" and calls["n"] == 2


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


def test_set_password_title_is_password_reset_not_blocked():
    def make(*a, **k):
        g = MagicMock()
        g.login.side_effect = garmin_login.GarminConnectConnectionError(
            "Widget login: unexpected title 'Set Password'")
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "pw", attempts=2, backoff=0, sleep=lambda s: None)
    assert ei.value.reason == "password_reset"


def test_auth_app_title_without_mfa_vars_is_auth_not_blocked():
    # garminconnect ≥0.3.14 turns this title + mfaMethod into needs_mfa; when
    # it reaches us as unexpected title the signin page came back without MFA
    # vars — treat as credentials, not an IP block.
    def make(*a, **k):
        g = MagicMock()
        g.login.side_effect = garmin_login.GarminConnectConnectionError(
            "Widget login: unexpected title 'GARMIN Authentication Application'")
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "pw", attempts=2, backoff=0, sleep=lambda s: None)
    assert ei.value.reason == "auth"


def test_skip_strategies_is_honored_by_installed_garminconnect():
    # Guards the Docker pin: garmin-mcp's ==0.3.2 downgrade made setattr a
    # silent no-op and mobile kept running on every proxied sign-in.
    assert garmin_login.supports_skip_strategies()


def test_start_login_sets_skip_strategies_on_client():
    seen = {}

    def make(*a, **k):
        g = MagicMock()
        g.client.skip_strategies = set()

        def login(*la, **lk):
            seen["skip"] = set(g.client.skip_strategies)
            raise garmin_login.GarminConnectAuthenticationError("stop")

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError):
            garmin_login.start_login(
                "me@x.cz", "pw",
                skip_strategies=garmin_login._SKIP_MOBILE_WHEN_PROXIED)
    assert seen["skip"] == set(garmin_login._SKIP_MOBILE_WHEN_PROXIED)


def test_verify_tokens_returns_name():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        name = garmin_login.verify_tokens('{"oauth":"tok"}')
    assert name == "Vaclav S"


def test_verify_tokens_succeeds_when_name_empty():
    # A valid, authenticated account can legitimately have an empty fullName
    # (garminconnect defaults fullName to ""). Successful login (no exception)
    # already proves authentication, so an empty name must NOT be rejected.
    def make(*a, **k):
        g = MagicMock()
        g.login.return_value = (None, None)
        g.get_full_name.return_value = ""
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        name = garmin_login.verify_tokens('{"oauth":"tok"}')
    assert name == ""


def test_verify_tokens_raises_when_login_fails():
    # A genuine auth failure (login raises) must still surface as GarminLoginError.
    def make(*a, **k):
        g = MagicMock()
        g.login.side_effect = garmin_login.GarminConnectAuthenticationError("401 Unauthorized")
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError):
            garmin_login.verify_tokens('{"oauth":"tok"}')
