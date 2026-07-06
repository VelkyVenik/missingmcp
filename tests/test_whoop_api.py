"""WhoopApi: upstream OAuth exchange + gateway-owned rotating token refresh.
Async functions are driven with asyncio.run — no async test plugin needed."""
import asyncio
import json
import time
import pytest
from missingmcp import store
from missingmcp.adapters.whoop.api import WhoopApi, WhoopAuthError
from missingmcp.config import load_config

KEY = "user@example.com"


def _cfg(fake):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                        "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                        "WHOOP_API_BASE": f"http://127.0.0.1:{fake.port}"})


def _blob(expires_in=3600, access="at-0", refresh="rt-0"):
    return {"access_token": access, "refresh_token": refresh,
            "expires_at": int(time.time()) + expires_in,
            "user_id": 123, "email": KEY}


def _seed(conn, cfg, blob):
    store.upsert_account(conn, "whoop", KEY, json.dumps(blob), cfg.gateway_secret)


def _token_calls(fake):
    return [b for m, p, _h, b in fake.calls if p == "/oauth/oauth2/token"]


def test_auth_url_contains_upstream_oauth_params(fake_whoop):
    api = WhoopApi(_cfg(fake_whoop))
    url = api.auth_url("state-12345678")
    assert url.startswith(f"http://127.0.0.1:{fake_whoop.port}/oauth/oauth2/auth?")
    assert "response_type=code" in url and "client_id=cid-1" in url
    assert "state=state-12345678" in url
    assert "redirect_uri=https%3A%2F%2Fgw.example.com%2Fwhoop%2Foauth%2Fcallback" in url
    assert "offline" in url and "read%3Arecovery" in url


def test_exchange_code_builds_blob(fake_whoop):
    api = WhoopApi(_cfg(fake_whoop))
    blob = asyncio.run(api.exchange_code("upstream-code"))
    assert blob["access_token"] == "at-1" and blob["refresh_token"] == "rt-1"
    assert blob["expires_at"] > time.time()
    _m, path, _h, body = fake_whoop.calls[-1]
    assert path == "/oauth/oauth2/token"
    assert b"grant_type=authorization_code" in body
    assert b"code=upstream-code" in body and b"client_secret=sec-1" in body
    assert b"redirect_uri=" in body


def test_get_with_fresh_token_skips_refresh(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.valid_tokens.add("at-0")
    blob = _blob(); _seed(conn, cfg, blob)
    status, payload = asyncio.run(api.get(conn, KEY, blob, "/v2/user/profile/basic"))
    assert status == 200 and payload["email"] == "User@Example.com"
    assert _token_calls(fake_whoop) == []


def test_stale_token_refreshes_and_persists_rotation(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    blob = _blob(expires_in=30)               # inside the 120s refresh margin
    _seed(conn, cfg, blob)
    status, _ = asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
    assert status == 200
    calls = _token_calls(fake_whoop)
    assert len(calls) == 1
    assert b"grant_type=refresh_token" in calls[0]
    assert b"refresh_token=rt-0" in calls[0] and b"scope=offline" in calls[0]
    stored = json.loads(store.get_account_tokens(conn, "whoop", KEY, cfg.gateway_secret))
    assert stored["access_token"] == "at-1" and stored["refresh_token"] == "rt-1"
    assert stored["email"] == KEY and stored["user_id"] == 123   # identity survives rotation


def test_concurrent_stale_calls_refresh_exactly_once(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    blob = _blob(expires_in=30)
    _seed(conn, cfg, blob)

    async def both():
        await asyncio.gather(api.get(conn, KEY, dict(blob), "/v2/cycle"),
                             api.get(conn, KEY, dict(blob), "/v2/recovery"))
    asyncio.run(both())
    assert len(_token_calls(fake_whoop)) == 1     # the lock serialized; waiter reused the row


def test_unexpected_401_forces_one_refresh_and_retry(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    blob = _blob()                                # looks fresh, but at-0 is not valid upstream
    _seed(conn, cfg, blob)
    status, _ = asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
    assert status == 200                          # refreshed to at-1, retried
    assert len(_token_calls(fake_whoop)) == 1


def test_refresh_invalid_grant_purges_the_dead_account(fake_whoop):
    # WHOOP API Terms of Use: on termination, stored content must be deleted —
    # invalid_grant means the member revoked us, so the blob and every gateway
    # Bearer token for the account go away.
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.refresh_fails = True
    blob = _blob(expires_in=30)
    _seed(conn, cfg, blob)
    store.create_access_token(conn, store.hash_token("tok-w"), "whoop", KEY, "c1")
    with pytest.raises(WhoopAuthError):
        asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
    assert store.get_account_tokens(conn, "whoop", KEY, cfg.gateway_secret) is None
    assert store.account_key_for_token_hash(conn, store.hash_token("tok-w")) is None


def test_transient_refresh_failure_keeps_the_account(fake_whoop):
    # A WHOOP 5xx is not a revocation — nothing may be deleted.
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.refresh_fails = True
    fake_whoop.refresh_fail_status = 503
    fake_whoop.refresh_fail_error = "temporarily_unavailable"
    blob = _blob(expires_in=30)
    _seed(conn, cfg, blob)
    with pytest.raises(WhoopAuthError):
        asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
    assert store.get_account_tokens(conn, "whoop", KEY, cfg.gateway_secret) is not None


def test_persistent_401_after_refresh_raises(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.reject_data_auth = True            # even freshly minted tokens bounce
    blob = _blob()
    _seed(conn, cfg, blob)
    with pytest.raises(WhoopAuthError):
        asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
