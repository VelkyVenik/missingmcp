"""telemetry.py: the no-op contract, capture/identify shapes, cookie parsing,
the log-tee loop guard, the web snippet — and the oauth funnel events wired
through a stubbed SDK client (the SDK itself is never exercised here)."""
import json
import re
import urllib.parse
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from missingmcp import log as mlog
from missingmcp import oauth, proxy, security, store, telemetry
from missingmcp.adapters.garmin import GarminAdapter, login as garmin_login
from missingmcp.config import load_config

CONFIG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com"})
PH_CONFIG = load_config({
    "GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com",
    "POSTHOG_API_KEY": "phc_test", "POSTHOG_WEB_HOST": "https://j.example.com",
})


class Recorder:
    def __init__(self):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None):
        self.events.append((event, distinct_id, properties))


@pytest.fixture
def recorder(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(telemetry, "_client", r)
    monkeypatch.setattr(telemetry, "_api_key", "phc_test")
    return r


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


# --- no-op contract ----------------------------------------------------------

def test_disabled_is_noop():
    assert not telemetry.enabled()
    telemetry.capture("x", distinct_id="a")          # must not raise
    telemetry.identify("a@b.c", "anon")
    assert telemetry.anon_id_from_cookie({"ph_phc_test_posthog": "{}"}) is None
    assert telemetry.web_head(CONFIG) == ""


def test_init_without_key_stays_disabled():
    telemetry.init(CONFIG)
    assert not telemetry.enabled()


# --- capture / identify ------------------------------------------------------

def test_capture_records_event(recorder):
    telemetry.capture("account_connected", distinct_id="me@x.cz",
                      properties={"adapter": "garmin", "status": "new"})
    assert recorder.events == [
        ("account_connected", "me@x.cz", {"adapter": "garmin", "status": "new"})]


def test_capture_anonymous_marks_personless(recorder):
    telemetry.capture("subscribe", anonymous=True)
    event, did, props = recorder.events[0]
    assert (event, did) == ("subscribe", None)
    assert props["$process_person_profile"] is False


def test_capture_swallows_sdk_errors(recorder):
    recorder.capture = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    telemetry.capture("x", distinct_id="a")          # must not raise


def test_identify_shape(recorder):
    telemetry.identify("me@x.cz", "anon-123")
    assert recorder.events == [
        ("$identify", "me@x.cz", {"$anon_distinct_id": "anon-123"})]


# --- cookie parsing ----------------------------------------------------------

def _ph_cookie(distinct_id):
    return urllib.parse.quote(json.dumps({"distinct_id": distinct_id}))


def test_anon_id_from_cookie(recorder):
    cookies = {"ph_phc_test_posthog": _ph_cookie("anon-123")}
    assert telemetry.anon_id_from_cookie(cookies) == "anon-123"


@pytest.mark.parametrize("cookies", [
    {},                                                # absent
    {"ph_phc_test_posthog": "not-json"},               # garbage
    {"ph_phc_test_posthog": "%7B%22a%22%3A1%7D"},      # JSON, no distinct_id
    {"ph_phc_other_posthog": _ph_cookie("anon-1")},    # different project's cookie
])
def test_anon_id_from_cookie_edge_cases(recorder, cookies):
    assert telemetry.anon_id_from_cookie(cookies) is None


# --- log tee -----------------------------------------------------------------

def test_tee_skips_export_path_loggers():
    assert telemetry._tee_wanted({"event": "mcp-response"})
    assert telemetry._tee_wanted({"event": "stdlib-log", "logger": "uvicorn.error"})
    for name in ("opentelemetry.sdk", "urllib3.connectionpool",
                 "requests.adapters", "posthog"):
        assert not telemetry._tee_wanted({"event": "stdlib-log", "logger": name})


def test_log_sink_sees_records_and_never_breaks_logging():
    got = []
    mlog.set_sink(got.append)
    try:
        mlog.log("telemetry-test", a=1)
        assert got and got[-1]["event"] == "telemetry-test" and got[-1]["a"] == 1
        mlog.set_sink(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
        mlog.log("still-works")                      # must not raise
    finally:
        mlog.set_sink(None)


# --- web snippet -------------------------------------------------------------

def test_web_head_uses_proxy_host(recorder):
    head = telemetry.web_head(PH_CONFIG)
    assert 'src="https://j.example.com/static/array.js"' in head
    assert 'src="/static/ph.js"' in head


def test_web_head_rewrites_cloud_host_to_assets(recorder):
    cfg = load_config({"GATEWAY_SECRET": "z" * 40, "POSTHOG_API_KEY": "phc_test"})
    assert "eu-assets.i.posthog.com/static/array.js" in telemetry.web_head(cfg)


def test_bootstrap_js_proxied(recorder):
    js = telemetry.web_bootstrap_js(PH_CONFIG)
    assert '"phc_test"' in js
    assert '"api_host": "https://j.example.com"' in js
    assert '"ui_host": "https://eu.posthog.com"' in js      # proxy needs ui_host
    assert "'/oauth/'" in js                                 # oauth pages branch


def test_bootstrap_js_direct_cloud_has_no_ui_host(recorder):
    cfg = load_config({"GATEWAY_SECRET": "z" * 40, "POSTHOG_API_KEY": "phc_test"})
    assert "ui_host" not in telemetry.web_bootstrap_js(cfg)


def test_csp_widens_only_with_hosts():
    base = security.security_headers()["Content-Security-Policy"]
    assert "script-src" not in base and "connect-src" not in base
    widened = security.security_headers(
        script_hosts=("https://j.example.com",),
        connect_hosts=("https://j.example.com",))["Content-Security-Policy"]
    assert "script-src 'self' https://j.example.com" in widened
    assert "connect-src 'self' https://j.example.com" in widened


# --- store helper ------------------------------------------------------------

def test_account_exists(conn):
    assert not store.account_exists(conn, "garmin", "me@x.cz")
    store.upsert_account(conn, "garmin", "me@x.cz", "{}", CONFIG.gateway_secret)
    assert store.account_exists(conn, "garmin", "me@x.cz")


# --- proxy event mapping -----------------------------------------------------

def test_mcp_event_tools_call():
    body = json.dumps({"method": "tools/call",
                       "params": {"name": "get_sleep_data",
                                  "arguments": {"secret": "no"}}}).encode()
    name, props = proxy._mcp_event(body, "garmin")
    assert name == "$mcp_tool_call"
    assert props["$mcp_tool_name"] == "get_sleep_data"
    assert props["$mcp_server_name"] == "missingmcp-garmin"
    assert "arguments" not in json.dumps(props)      # content never leaves


def test_mcp_event_initialize_and_list():
    body = json.dumps({"method": "initialize", "params": {
        "clientInfo": {"name": "claude-ai", "version": "0.1"}}}).encode()
    name, props = proxy._mcp_event(body, "whoop")
    assert name == "$mcp_initialize"
    assert props["$mcp_client_name"] == "claude-ai"
    assert props["$mcp_client_version"] == "0.1"
    assert proxy._mcp_event(json.dumps({"method": "tools/list"}).encode(), "whoop")[0] == "$mcp_tools_list"


@pytest.mark.parametrize("body", [b"", b"junk", b"[]",
                                  json.dumps({"method": "ping"}).encode()])
def test_mcp_event_ignores_other_bodies(body):
    assert proxy._mcp_event(body, "garmin") is None


def test_capture_mcp_error_props(recorder):
    proxy._capture_mcp(("$mcp_tool_call", {"$mcp_tool_name": "t", "adapter": "garmin"}),
                       "me@x.cz", 502, 10, 20, 33)
    event, did, props = recorder.events[0]
    assert (event, did) == ("$mcp_tool_call", "me@x.cz")
    assert props["$mcp_is_error"] is True and props["$mcp_error_status"] == 502
    assert props["$mcp_duration_ms"] == 20 and props["ttfb_ms"] == 10 and props["bytes"] == 33


# --- oauth funnel events (flow-level) -----------------------------------------

def _authz_app(conn):
    adapter = GarminAdapter(CONFIG)
    state = oauth.AuthState(security.CsrfStore())

    async def apost(request):
        return await oauth.authorize_post(request, adapter, state, conn, CONFIG)
    app = Starlette(routes=[Route("/oauth/authorize", apost, methods=["POST"])])
    return TestClient(app, follow_redirects=False), state


def test_login_flow_emits_funnel_events_and_stitch(conn, recorder):
    client, state = _authz_app(conn)
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude", "garmin")
    client.cookies.set("ph_phc_test_posthog", _ph_cookie("anon-123"))
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="ok", tokens_json='{"t":1}')), \
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"):
        r = client.post("/oauth/authorize", data={
            "csrf": state.csrf.issue(), "client_id": cid,
            "redirect_uri": "https://claude.ai/cb", "state": "xyz",
            "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "Me@X.cz", "garmin_password": "pw",
        })
    assert r.status_code == 302
    by_name = {e: (d, p) for e, d, p in recorder.events}
    assert by_name["login_succeeded"][0] == "me@x.cz"
    assert by_name["account_connected"] == ("me@x.cz", {"adapter": "garmin", "status": "new"})
    assert by_name["$identify"] == ("me@x.cz", {"$anon_distinct_id": "anon-123"})


def test_login_failure_emits_login_failed(conn, recorder):
    client, state = _authz_app(conn)
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude", "garmin")
    with patch.object(garmin_login, "start_login",
                      side_effect=garmin_login.GarminLoginError("bad credentials")):
        r = client.post("/oauth/authorize", data={
            "csrf": state.csrf.issue(), "client_id": cid,
            "redirect_uri": "https://claude.ai/cb", "state": "xyz",
            "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r.status_code == 200                     # form re-rendered
    events = [e for e, _, _ in recorder.events]
    assert events == ["login_failed"]
    _, did, props = recorder.events[0]
    # personless by design: the form email is unverified — it must never
    # become a PostHog person via a failure event
    assert did is None and props["adapter"] == "garmin" and props["reason"]


def test_returning_account_status(conn, recorder):
    client, state = _authz_app(conn)
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude", "garmin")
    store.upsert_account(conn, "garmin", "me@x.cz", "{}", CONFIG.gateway_secret)
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="ok", tokens_json='{"t":2}')), \
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"):
        r = client.post("/oauth/authorize", data={
            "csrf": state.csrf.issue(), "client_id": cid,
            "redirect_uri": "https://claude.ai/cb", "state": "xyz",
            "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r.status_code == 302
    by_name = {e: p for e, _, p in recorder.events}
    assert by_name["account_connected"]["status"] == "returning"
