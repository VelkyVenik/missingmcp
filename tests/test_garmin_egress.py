import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from garminconnect import client as gc_client

from unittest.mock import patch

from missingmcp.adapters import base
from missingmcp.adapters.garmin import EgressPool, GarminAdapter, egress, login
from missingmcp.adapters.garmin.probe import SsoProbe
from missingmcp.config import load_config

PROXY = "http://user:s3cret@proxy.example:3128"
P1 = "http://u:pw@10.0.0.1:3128"
P2 = "http://u:pw@10.0.0.2:3129"
FORM = {"garmin_email": "me@x.cz", "garmin_password": "pw"}


def _cfg(**env):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x", **env})


def _rows(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


@pytest.fixture
def recording_proxy():
    """A plain-HTTP forward proxy stand-in: records the request line it gets
    (a proxied request carries the absolute URI) and answers 200."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_proxy_is_off_by_default():
    cfg = _cfg()
    assert cfg.garmin_sso_proxy == ""
    a = GarminAdapter(cfg)
    assert [r.label for r in a.pool.routes] == ["direct"]
    assert gc_client.requests.Session().proxies == {}


def test_describe_never_leaks_credentials():
    assert egress.describe(PROXY) == "proxy.example:3128"
    assert egress.describe(None) == "direct"


def test_adapter_builds_routes_and_logs_labels_only(capsys):
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f" {P1} , {P2} "))
    assert [(r.label, r.proxy) for r in a.pool.routes] == [
        ("10.0.0.1:3128", P1), ("10.0.0.2:3129", P2)]
    out = capsys.readouterr().out
    assert "pw@" not in out
    row = [json.loads(line) for line in out.splitlines() if '"garmin-sso-proxy"' in line][0]
    assert row["egress"] == "10.0.0.1:3128,10.0.0.2:3129"
    assert "10.0.0.1:3128" in row["message"]


def test_accounts_stick_to_one_proxy_and_spread_across_them():
    pool = EgressPool(f"{P1},{P2}")
    picks = {k: pool.pick(k).label for k in (f"user{i}@x.cz" for i in range(40))}
    assert picks == {k: pool.pick(k).label for k in picks}          # sticky
    assert set(picks.values()) == {"10.0.0.1:3128", "10.0.0.2:3129"}  # spread
    assert all("direct" not in {r.label for r in pool.order(k)} for k in picks)


def test_blocked_proxy_sheds_its_accounts_to_the_next_route():
    pool = EgressPool(f"{P1},{P2}")
    acct = "me@x.cz"
    first, second = pool.order(acct)
    first.breaker.trip()
    assert pool.pick(acct) is second
    second.breaker.trip()
    assert pool.pick(acct) is None   # no direct fallback when proxies are set


def test_proxied_pool_never_includes_direct():
    pool = EgressPool(f"{P1},{P2}")
    assert [r.label for r in pool.routes] == ["10.0.0.1:3128", "10.0.0.2:3129"]
    assert pool.pick("anyone@x.cz").proxy is not None


def _route_spy(outcome):
    seen = []

    def fake(email, pw, **_kw):
        seen.append(egress.current())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return seen, fake


def test_sign_in_runs_through_the_accounts_route_and_logs_it(capsys):
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    route = a.pool.pick("me@x.cz")
    seen, fake = _route_spy(login.LoginResult(status="ok", tokens_json='{"t":1}'))
    with patch.object(login, "start_login", side_effect=fake):
        assert isinstance(a.start_login(FORM), base.LoginOk)
    assert seen == [route.proxy]
    assert egress.current() is None                 # nothing leaks past the call
    rows = [r for r in _rows(capsys) if r.get("event") == "garmin-login-attempt"]
    assert rows[-1]["egress"] == route.label and rows[-1]["outcome"] == "ok"
    assert rows[-1]["account"] == "me@x.cz"         # per-user attribution
    assert rows[-1]["message"] == f"garmin login via {route.label}: ok"


def test_proxied_sign_in_skips_mobile_strategies():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=P1))
    seen = {}

    def fake(email, pw, skip_strategies=None):
        seen["skip"] = skip_strategies
        return login.LoginResult(status="ok", tokens_json='{"t":1}')
    with patch.object(login, "start_login", side_effect=fake):
        a.start_login(FORM)
    assert seen["skip"] == login._SKIP_MOBILE_WHEN_PROXIED


def test_direct_sign_in_does_not_skip_mobile_strategies():
    a = GarminAdapter(_cfg())
    seen = {}

    def fake(email, pw, skip_strategies=None):
        seen["skip"] = skip_strategies
        return login.LoginResult(status="ok", tokens_json='{"t":1}')
    with patch.object(login, "start_login", side_effect=fake):
        a.start_login(FORM)
    assert seen["skip"] is None


def _accounts_on(pool, route, n):
    """n distinct accounts whose sticky (first) route is `route`."""
    out = []
    for i in range(1000):
        acct = f"user{i}@x.cz"
        if pool.order(acct)[0] is route:
            out.append(acct)
            if len(out) == n:
                return out
    raise AssertionError("not enough accounts hashed onto the route")


def test_one_blocked_account_cools_down_only_itself(capsys):
    # Garmin limits repeat sign-ins of one account regardless of IP; that
    # must not close the egress for everyone else on it.
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    first = a.pool.order("me@x.cz")[0]
    other = _accounts_on(a.pool, first, 2)[1]
    seen, fake = _route_spy(login.GarminLoginError("429", reason="blocked"))
    with patch.object(login, "start_login", side_effect=fake):
        for _ in range(2):
            with pytest.raises(base.LoginError) as ei:
                a.start_login(FORM)
            assert ei.value.reason == "blocked"
    assert seen == [first.proxy]                    # the retry never reached Garmin
    assert first.breaker.remaining() == 0
    assert a.pool.pick(other) is first              # others keep using the egress
    rows = _rows(capsys)
    opened = [(r["scope"], r["egress"]) for r in rows if r.get("event") == "login-breaker-open"]
    assert opened == [("account", first.label)]
    rejects = [(r["scope"], r["account"]) for r in rows if r.get("event") == "login-breaker-reject"]
    assert rejects == [("account", "me@x.cz")]


def test_distinct_blocked_accounts_close_the_egress_and_shed_its_accounts(capsys):
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    first = a.pool.routes[0]
    one, two, three = _accounts_on(a.pool, first, 3)
    seen, fake = _route_spy(login.GarminLoginError("429", reason="blocked"))
    with patch.object(login, "start_login", side_effect=fake):
        for acct in (one, two):
            with pytest.raises(base.LoginError):
                a.start_login({"garmin_email": acct, "garmin_password": "pw"})
    assert first.breaker.remaining() > 0            # 2 distinct accounts: IP-level limit
    assert a.pool.pick(three) is not first          # its accounts move to the next route
    opened = [r["scope"] for r in _rows(capsys) if r.get("event") == "login-breaker-open"]
    assert opened == ["account", "egress"]


def test_old_blocks_age_out_of_the_egress_window():
    clock = [0.0]
    pool = EgressPool(f"{P1},{P2}", clock=lambda: clock[0])
    route = pool.routes[0]
    one, two = _accounts_on(pool, route, 2)
    assert pool.record_blocked(route, one) == "account"
    clock[0] += egress._EGRESS_BLOCK_WINDOW_S + 1
    assert pool.record_blocked(route, two) == "account"   # the first block is stale
    assert route.breaker.remaining() == 0


def test_mfa_resume_leaves_through_the_route_the_sign_in_used():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    route = a.pool.pick("me@x.cz")
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        need = a.start_login(FORM)
    for r in a.pool.routes:                         # breakers don't gate MFA
        r.breaker.trip()
    seen = []

    def resume(pending, code):
        seen.append(egress.current())
        return '{"t":9}'
    with patch.object(login, "resume_login", side_effect=resume):
        ok = a.resume_second_factor(need.state, {"mfa_code": "123456"})
    assert ok == base.LoginOk(account_key="me@x.cz", blob='{"t":9}')
    assert seen == [route.proxy]


def test_via_is_thread_local():
    seen = {}

    def other():
        seen["other"] = egress.current()
    with egress.via(P1):
        t = threading.Thread(target=other)
        t.start()
        t.join()
        seen["here"] = egress.current()
    assert seen == {"other": None, "here": P1}


def test_garminconnect_sessions_get_the_threads_proxy():
    with egress.via(PROXY):
        s = gc_client.requests.Session()
    assert s.proxies == {"http": PROXY, "https": PROXY}
    # The wrappers stay drop-in for the rest of the module API garminconnect uses.
    assert gc_client.requests.adapters.HTTPAdapter is not None
    assert gc_client.requests.Session().proxies == {}


def test_cffi_wrapper_injects_proxy_into_sessions_and_posts():
    calls = []

    class FakeCffi:
        def Session(self, *a, **kw):  # noqa: N802
            calls.append(("Session", kw))

        def post(self, url, **kw):
            calls.append(("post", kw))

    w = egress._ProxiedCffi(FakeCffi())
    w.Session(impersonate="chrome")
    with egress.via(PROXY):
        w.Session(impersonate="chrome")
        w.post("https://diauth.garmin.com/x", impersonate="chrome")
    assert calls == [
        ("Session", {"impersonate": "chrome"}),
        ("Session", {"impersonate": "chrome", "proxy": PROXY}),
        ("post", {"impersonate": "chrome", "proxy": PROXY}),
    ]


def test_requests_traffic_really_goes_through_the_proxy(recording_proxy):
    url, seen = recording_proxy
    with egress.via(url):
        r = gc_client.requests.Session().get("http://sso.garmin.invalid/portal", timeout=5)
    assert r.status_code == 200
    assert seen == ["http://sso.garmin.invalid/portal"]


def test_chosen_proxy_beats_env_proxies(recording_proxy, monkeypatch):
    # requests lets HTTP(S)_PROXY override Session.proxies unless trust_env is
    # off — the route that logs/trips breakers must be the one actually used.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    url, seen = recording_proxy
    with egress.via(url):
        s = gc_client.requests.Session()
    r = s.get("http://sso.garmin.invalid/portal", timeout=5)
    assert r.status_code == 200
    assert seen == ["http://sso.garmin.invalid/portal"]


def test_cffi_traffic_really_goes_through_the_proxy(recording_proxy):
    url, seen = recording_proxy
    with egress.via(url):
        s = gc_client.cffi_requests.Session(impersonate="chrome")
    r = s.get("http://sso.garmin.invalid/portal", timeout=5)
    assert r.status_code == 200
    assert seen == ["http://sso.garmin.invalid/portal"]


def test_probe_checks_every_egress_and_labels_it(capsys):
    pool = EgressPool(f"{P1},{P2}")
    asked = []

    def fetch(proxy=None):
        asked.append(proxy)
        return 429 if proxy is None else 200
    SsoProbe(600, fetch=fetch, routes=pool.routes).run()
    rows = [r for r in _rows(capsys) if r.get("event") == "sso-probe"]
    assert asked == [P1, P2]                        # no direct probe when proxied
    assert [(r["via"], r["status"]) for r in rows] == [
        ("10.0.0.1:3128", 200), ("10.0.0.2:3129", 200)]
    assert all("sso-probe via" in r["message"] for r in rows)
