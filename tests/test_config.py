import pytest
from garmin_gateway.config import load_config

BASE = {"GATEWAY_SECRET": "x" * 32, "PUBLIC_URL": "https://gw.example.com"}

def test_loads_defaults():
    c = load_config(BASE)
    assert c.public_url == "https://gw.example.com"
    assert c.port == 8080
    assert c.worker_port_start == 9000
    assert c.worker_idle_ttl == 900
    assert c.garmin_mcp_cmd == ["garmin-mcp"]
    assert c.rohlik_mcp_url == "https://mcp.rohlik.cz/mcp"

def test_rohlik_mcp_url_override():
    c = load_config({**BASE, "ROHLIK_MCP_URL": "http://127.0.0.1:9999/mcp"})
    assert c.rohlik_mcp_url == "http://127.0.0.1:9999/mcp"

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
