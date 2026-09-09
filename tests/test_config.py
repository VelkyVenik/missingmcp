import pytest
from garmin.config import load_config

BASE = {"GATEWAY_SECRET": "x" * 32, "PUBLIC_URL": "https://gw.example.com"}

def test_loads_defaults():
    c = load_config(BASE)
    assert c.public_url == "https://gw.example.com"
    assert c.port == 8080
    assert c.worker_port_start == 9000
    assert c.worker_idle_ttl == 900
    assert c.garmin_mcp_cmd == ["garmin-mcp"]


def test_orphan_client_ttl_default_is_thirty_days():
    # A 0-token OAuth client older than this is an abandoned DCR and gets swept.
    # Intentionally not env-configurable (see docs/adr/0001) — a hardcoded default.
    # DAYS, not hours: Claude/ChatGPT cache their DCR client per org and re-use
    # it for later re-authorizations; a short TTL strands users on
    # "unknown client_id" (observed in production, 2026-07-17).
    c = load_config(BASE)
    assert c.orphan_client_ttl == 30 * 86400


def test_login_timeout_default_is_30s():
    # Wall-clock cap on a synchronous adapter sign-in (garminconnect), so a
    # rate-limited Garmin login can't block the request (and the event loop) for
    # minutes. Hardcoded, not env-configurable.
    c = load_config(BASE)
    assert c.login_timeout == 30

def test_strips_trailing_slash_from_public_url():
    c = load_config({**BASE, "PUBLIC_URL": "https://gw.example.com/"})
    assert c.public_url == "https://gw.example.com"

def test_rejects_short_secret():
    with pytest.raises(ValueError):
        load_config({"GATEWAY_SECRET": "short"})

def test_rejects_missing_secret():
    with pytest.raises(ValueError):
        load_config({})

def test_rejects_placeholder_secret():
    with pytest.raises(ValueError):
        load_config({"GATEWAY_SECRET": "change-me-to-a-long-random-string-min-32-chars",
                     "PUBLIC_URL": "https://gw.example.com"})
