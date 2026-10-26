import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from garminconnect import client as gc_client

from unittest.mock import patch

from missingmcp.adapters import base
from missingmcp.adapters.garmin import EgressPool, GarminAdapter, SsoBreaker, egress, login
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
        ("10.0.0.1:3128", P1), ("10.0.0.2:3129", P2), ("direct", None)]
    out = capsys.readouterr().out
    assert "pw@" not in out
    row = [json.loads(line) for line in out.splitlines() if '"garmin-sso-proxy"' in line][0]
    assert row["egress"] == "10.0.0.1:3128,10.0.0.2:3129,direct"
    assert "10.0.0.1:3128" in row["message"]


def test_accounts_stick_to_one_proxy_and_spread_across_them():
    pool = EgressPool(f"{P1},{P2}")
    picks = {k: pool.pick(k).label for k in (f"user{i}@x.cz" for i in range(40))}
    assert picks == {k: pool.pick(k).label for k in picks}          # sticky
    assert set(picks.values()) == {"10.0.0.1:3128", "10.0.0.2:3129"}  # spread
    assert all(pool.order(k)[-1].label == "direct" for k in picks)  # direct last


def test_blocked_proxy_sheds_its_accounts_to_the_next_route():
    pool = EgressPool(f"{P1},{P2}")
    acct = "me@x.cz"
    first, second, direct = pool.order(acct)
    first.breaker.trip()
    assert pool.pick(acct) is second
    second.breaker.trip()
    assert pool.pick(acct) is direct                # last resort, not "no sign-ins"
    direct.breaker.trip()
    assert pool.pick(acct) is None


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


def test_proxied_sign_in_skips_mobile_and_portal_strategies():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=P1))
    seen = {}

    def fake(email, pw, skip_strategies=None):
        seen["skip"] = skip_strategies
        return login.LoginResult(status="ok", tokens_json='{"t":1}')
    with patch.object(login, "start_login", side_effect=fake):
        a.start_login(FORM)
    assert seen["skip"] == login._SKIP_WHEN_PROXIED
    assert {"mobile+cffi", "portal+requests"} <= seen["skip"]
    assert "widget+cffi" not in seen["skip"]       # the one strategy that works


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
    second = a.pool.order(two)[1]
    assert seen == [first.proxy, first.proxy, second.proxy]   # the trip failed `two` over
    assert second.breaker.remaining() == 0          # a failed-over block is the account's
    opened = [(r["scope"], r["egress"]) for r in _rows(capsys)
              if r.get("event") == "login-breaker-open"]
    assert opened == [("account", first.label), ("egress", first.label),
                      ("account", second.label)]


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
    SsoProbe(600, fetch=fetch, routes=pool.routes, trace=lambda proxy=None: None).run()
    rows = [r for r in _rows(capsys) if r.get("event") == "sso-probe"]
    assert asked == [P1, P2, None]                  # direct kept as the baseline
    assert [(r["via"], r["status"]) for r in rows] == [
        ("10.0.0.1:3128", 200), ("10.0.0.2:3129", 200), ("direct", 429)]
    assert all("sso-probe via" in r["message"] for r in rows)


def test_a_sign_in_getting_through_resets_the_egress_block_count():
    # Healthy IP, routine per-account blocks around a success: account-level,
    # so the egress must stay open (2026-10-05: :3128 kept 37 good / 15 blocked).
    pool = EgressPool(f"{P1},{P2}")
    route = pool.routes[0]
    one, two = _accounts_on(pool, route, 2)
    assert pool.record_blocked(route, one) == "account"
    pool.record_ok(route)
    assert pool.record_blocked(route, two) == "account"
    assert route.breaker.remaining() == 0
    # ...while blocks with nobody getting through in between still trip it.
    three = _accounts_on(pool, route, 3)[2]
    assert pool.record_blocked(route, three) == "egress"


def test_probe_learns_each_egress_ip_and_sign_ins_carry_it(capsys):
    # Every port shares the proxy host in its label; the probe learns the IP
    # each one really leaves from, and sign-in events carry it.
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    ips = {P1: "203.0.113.1", P2: "203.0.113.2", None: "198.51.100.9"}
    SsoProbe(600, fetch=lambda proxy=None: 200, pool=a.pool,
             trace=lambda proxy=None: ips[proxy]).run()
    assert [r.egress_ip for r in a.pool.routes] == ["203.0.113.1", "203.0.113.2", "198.51.100.9"]
    route = a.pool.pick("me@x.cz")
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json='{"t":1}')):
        a.start_login(FORM)
    att = [r for r in _rows(capsys) if r.get("event") == "garmin-login-attempt"][-1]
    assert att["egress_ip"] == route.egress_ip


def test_probe_keeps_the_last_known_ip_and_logs_a_change(capsys):
    pool = EgressPool(P1)
    answers = iter(["203.0.113.1", None, "203.0.113.7"])
    p = SsoProbe(600, fetch=lambda proxy=None: 200, pool=pool,
                 trace=lambda proxy=None: next(answers) if proxy else None)
    p.run()
    p.run()                                          # trace failed: keep the old IP
    assert pool.routes[0].egress_ip == "203.0.113.1"
    p.run()
    assert pool.routes[0].egress_ip == "203.0.113.7"
    changed = [r for r in _rows(capsys) if r.get("event") == "egress-ip-changed"]
    assert [(c["previous"], c["egress_ip"]) for c in changed] == [("203.0.113.1", "203.0.113.7")]


def test_probe_reports_real_sign_in_outcomes_per_egress_and_resets(capsys):
    # The embed GET can't see a blocked credential POST; egress-health counts
    # what real sign-ins got since the previous run, with no extra traffic.
    pool = EgressPool(f"{P1},{P2}")
    first, second, direct = pool.routes
    for outcome in ("ok", "needs_mfa", "blocked", "blocked", "unknown"):
        pool.note(first, outcome)
    pool.note(second, "ok")
    p = SsoProbe(600, fetch=lambda proxy=None: 200, pool=pool, trace=lambda proxy=None: None)
    p.run()
    health = {r["via"]: r for r in _rows(capsys) if r.get("event") == "egress-health"}
    h = health[first.label]
    assert (h["attempts"], h["ok"], h["needs_mfa"], h["blocked"], h["other"]) == (5, 1, 1, 2, 1)
    assert health[second.label]["attempts"] == 1 and health["direct"]["attempts"] == 0
    p.run()                                           # counters reset per run
    again = {r["via"]: r for r in _rows(capsys) if r.get("event") == "egress-health"}
    assert all(r["attempts"] == 0 for r in again.values())


# ---- failing-logins map: "Choose the throttle policy" ------------------------

def test_one_in_flight_sign_in_per_email(capsys):
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=P1))
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(email, pw, **_kw):
        calls.append(email)
        started.set()
        release.wait(5)
        return login.LoginResult(status="needs_mfa", pending=("P", "S"))

    with patch.object(login, "start_login", side_effect=slow):
        t = threading.Thread(target=lambda: a.start_login(FORM))
        t.start()
        assert started.wait(5)
        with pytest.raises(base.LoginError) as ei:       # concurrent second submit
            a.start_login(FORM)
        assert "already in progress" in str(ei.value)
        release.set()
        t.join(5)
        a.start_login(FORM)                               # slot freed once Garmin answered
    assert calls == ["me@x.cz", "me@x.cz"]
    rejects = [r for r in _rows(capsys) if r.get("event") == "login-breaker-reject"]
    assert [r["scope"] for r in rejects] == ["in_flight"]


def test_in_flight_slot_is_freed_on_failure_too():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=P1))
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("x", reason="unknown")):
        with pytest.raises(base.LoginError):
            a.start_login(FORM)
    assert a.pool.begin("me@x.cz")                       # nothing left holding it


def test_post_connect_hold_after_a_real_connection_only(capsys):
    clock = [1000.0]
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=P1))
    a.pool = EgressPool(P1, clock=lambda: clock[0])
    # reaching MFA alone (what a wrong password looks like) starts no hold
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        need = a.start_login(FORM)
        a.start_login(FORM)
    # finishing MFA is a real connection: the next sign-in is held
    with patch.object(login, "resume_login", return_value='{"t":9}'):
        a.resume_second_factor(need.state, {"mfa_code": "123456"})
    with pytest.raises(base.LoginError) as ei:
        a.start_login(FORM)
    assert "just connected" in str(ei.value)
    clock[0] += egress.POST_CONNECT_HOLD_S + 1
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json='{"t":1}')):
        a.start_login(FORM)                               # hold over
        with pytest.raises(base.LoginError):              # and an ok starts a new one
            a.start_login(FORM)
    scopes = [r["scope"] for r in _rows(capsys) if r.get("event") == "login-breaker-reject"]
    assert scopes == ["post_success", "post_success"]


def test_ten_blocks_in_an_hour_trip_an_egress_even_with_successes_between():
    clock = [0.0]
    pool = EgressPool(f"{P1},{P2}", clock=lambda: clock[0])
    route = pool.routes[0]
    accts = _accounts_on(pool, route, 10)
    for i, acct in enumerate(accts):
        pool.record_ok(route)                            # healthy-looking traffic
        clock[0] += 300                                  # 10 blocks over 50 min
        scope = pool.record_blocked(route, acct)
        assert scope == ("egress" if i == 9 else "account")
    assert route.breaker.remaining() > 0


def test_egress_rest_escalates_and_resets_after_a_sign_in_gets_through():
    clock = [0.0]
    pool = EgressPool(f"{P1},{P2}", breaker=lambda: SsoBreaker(clock=lambda: clock[0]),
                      clock=lambda: clock[0])
    route = pool.routes[0]
    accts = _accounts_on(pool, route, 8)

    def trip(i):
        assert pool.record_blocked(route, accts[i]) == "egress"
        return route.rest_s

    pool.record_blocked(route, accts[0])
    assert trip(1) == 1800                              # 2 distinct accounts
    for i, step in enumerate((7200, 21600, 86400, 86400), start=2):
        clock[0] += route.rest_s + 1
        assert trip(i) == step                          # suspect: one block is enough
    clock[0] += 86401
    pool.record_ok(route)                               # recovered: back to step one
    assert pool.record_blocked(route, accts[6]) == "account"
    assert trip(7) == 1800


def test_wrong_credentials_neither_cool_the_account_nor_count_against_the_egress(capsys):
    # A re-rendered sign-in form is Garmin answering normally: the user fixes
    # the typo and retries right away; the egress is evidently healthy.
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=P1))
    route = a.pool.pick("me@x.cz")
    err = login.GarminLoginError("x", reason="auth",
                                 page="GARMIN Authentication Application")
    with patch.object(a.pool, "record_ok", wraps=a.pool.record_ok) as ok, \
            patch.object(login, "start_login", side_effect=err):
        with pytest.raises(base.LoginError) as ei:
            a.start_login(FORM)
    assert ei.value.reason == "auth"
    ok.assert_called_once_with(route)
    assert a.pool.account_remaining("me@x.cz") == 0
    rows = _rows(capsys)
    assert not [r for r in rows if r.get("event") == "login-breaker-open"]
    att = [r for r in rows if r.get("event") == "garmin-login-attempt"][-1]
    assert att["outcome"] == "auth" and att["page"] == "GARMIN Authentication Application"


def test_dead_proxy_fails_over_within_the_same_sign_in(capsys):
    # 2026-10-16..18: three of four proxy ports lost their floating IPs; every
    # sign-in on them read "Garmin is rate-limiting" and locked the account.
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    first, second, _direct = a.pool.order("me@x.cz")
    seen = []

    def fake(email, pw, **_kw):
        seen.append(egress.current())
        if egress.current() == first.proxy:
            raise login.GarminLoginError("CONNECT tunnel failed", reason="egress")
        return login.LoginResult(status="ok", tokens_json='{"t":1}')
    with patch.object(login, "start_login", side_effect=fake):
        assert isinstance(a.start_login(FORM), base.LoginOk)
    assert seen == [first.proxy, second.proxy]
    assert a.pool.account_remaining("me@x.cz") == 0         # account not locked
    assert first.breaker.remaining() == pytest.approx(egress.EGRESS_DOWN_REST_S, abs=5)
    assert a.pool.pick("someone@x.cz") is not first        # nobody else tries it
    rows = _rows(capsys)
    outcomes = [(r["egress"], r["outcome"]) for r in rows if r.get("event") == "garmin-login-attempt"]
    assert outcomes == [(first.label, "egress"), (second.label, "ok")]
    opened = [(r["scope"], r["egress"]) for r in rows if r.get("event") == "login-breaker-open"]
    assert opened == [("egress_down", first.label)]


def test_egress_down_leaves_the_garmin_block_accounting_alone():
    pool = EgressPool(P1)
    route = pool.routes[0]
    pool.record_blocked(route, "a@x.cz")
    pool.record_down(route)
    pool.record_blocked(route, "b@x.cz")       # second distinct account: trips as before
    assert route.rest_s == egress._EGRESS_REST_STEPS_S[0]


def test_every_route_down_says_so_instead_of_blaming_garmin():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    seen, fake = _route_spy(login.GarminLoginError("curl: (7)", reason="egress"))
    with patch.object(login, "start_login", side_effect=fake):
        with pytest.raises(base.LoginError) as ei:
            a.start_login(FORM)
    assert ei.value.reason == "egress" and "not Garmin" in str(ei.value)
    assert len(seen) == 3                                   # both proxies, then direct
    assert a.pool.account_remaining("me@x.cz") == 0
    assert a.pool.begin("me@x.cz")                          # in-flight slot released


def test_egress_down_never_shortens_a_running_garmin_block_rest():
    # Review on 36eeae5: a concurrent sign-in that finished later with a proxy
    # failure cut a 30-min Garmin-block rest down to 5 min.
    pool = EgressPool(P1)
    route = pool.routes[0]
    pool.record_blocked(route, "a@x.cz")
    pool.record_blocked(route, "b@x.cz")                    # trips: 1800 s rest
    assert route.breaker.remaining() == pytest.approx(1800, abs=5)
    assert pool.record_down(route) == pytest.approx(1800, abs=5)
    assert route.breaker.remaining() == pytest.approx(1800, abs=5)
    assert route.rest_s == pytest.approx(1800, abs=5)


def test_breaker_extend_only_lengthens():
    t = [0.0]
    b = SsoBreaker(clock=lambda: t[0])
    assert b.extend(300) == 300
    b.trip(1800)
    assert b.extend(300) == 1800 and b.remaining() == 1800
    t[0] = 1700
    assert b.extend(300) == 300                             # longer than the 100 s left


def _persisting_pool(spec, rows=None, clock=None, wall=None):
    clock = clock or [0.0]
    wall = wall or [1_000_000.0]
    pool = EgressPool(spec, breaker=lambda: SsoBreaker(clock=lambda: clock[0]),
                      clock=lambda: clock[0], wall=lambda: wall[0])
    saved = {}
    pool.attach(rows or {}, lambda row: saved.__setitem__(row["label"], dict(row)))
    return pool, saved


def test_egress_rest_survives_a_restart(capsys):
    # hydra-ops 02: every deploy handed a burned IP back to users as "fresh".
    wall = [1_000_000.0]
    pool, saved = _persisting_pool(f"{P1},{P2}", wall=wall)
    route = pool.routes[0]
    a, b = _accounts_on(pool, route, 2)
    pool.record_blocked(route, a)
    pool.record_blocked(route, b)                       # trips: 30 min, level 1
    row = saved[route.label]
    assert row["rest_level"] == 1 and row["rest_until"] == wall[0] + 1800
    assert row["last_blocked_at"] == wall[0]

    wall[0] += 600                                      # restart 10 min later
    again, _ = _persisting_pool(f"{P1},{P2}", rows=dict(saved), wall=wall)
    r2 = again.routes[0]
    assert r2.breaker.remaining() == pytest.approx(1200)
    assert again.suspect(r2) and not again.suspect(again.routes[1])
    restored = [r for r in _rows(capsys) if r.get("event") == "egress-state-restored"]
    assert [(r["egress"], r["rest_level"], r["remaining_s"]) for r in restored] == \
        [(route.label, 1, 1200)]


def test_an_expired_rest_is_not_resumed_but_the_level_is():
    wall = [1_000_000.0]
    rows = {"10.0.0.1:3128": {"label": "10.0.0.1:3128", "egress_ip": "1.2.3.4",
                              "rest_level": 3, "rest_until": wall[0] - 5,
                              "last_ok_at": None, "last_blocked_at": None}}
    pool, saved = _persisting_pool(f"{P1},{P2}", rows=rows, wall=wall)
    route = pool.routes[0]
    assert route.breaker.remaining() == 0 and route.egress_ip == "1.2.3.4"
    assert pool.record_blocked(route, "x@x.cz") == "egress"   # still suspect
    assert route.rest_s == 86400 and saved[route.label]["rest_level"] == 4


def test_a_sign_in_through_clears_the_persisted_rest():
    wall = [1_000_000.0]
    pool, saved = _persisting_pool(P1, wall=wall)
    route = pool.routes[0]
    pool.record_blocked(route, "a@x.cz")
    pool.record_blocked(route, "b@x.cz")
    wall[0] += 2000
    pool.record_ok(route)
    row = saved[route.label]
    assert (row["rest_level"], row["rest_until"], row["last_ok_at"]) == (0, None, wall[0])


def test_a_new_egress_ip_starts_its_rest_over(capsys):
    # A burned floating IP swapped for a fresh one behind the same port.
    pool, saved = _persisting_pool(P1)
    route = pool.routes[0]
    route.egress_ip = "1.1.1.1"
    pool.record_blocked(route, "a@x.cz")
    pool.record_blocked(route, "b@x.cz")
    assert route.breaker.remaining() > 0
    probe = SsoProbe(60, pool=pool, fetch=lambda proxy=None: 200,
                     trace=lambda proxy=None: "2.2.2.2")
    probe.run()
    assert route.breaker.remaining() == 0 and not pool.suspect(route)
    assert saved[route.label]["egress_ip"] == "2.2.2.2"
    assert saved[route.label]["rest_level"] == 0
    assert any(r.get("event") == "egress-ip-changed" for r in _rows(capsys))


def test_the_same_egress_ip_keeps_its_rest():
    pool, saved = _persisting_pool(P1)
    route = pool.routes[0]
    route.egress_ip = "1.1.1.1"
    pool.record_blocked(route, "a@x.cz")
    pool.record_blocked(route, "b@x.cz")
    SsoProbe(60, pool=pool, fetch=lambda proxy=None: 200,
             trace=lambda proxy=None: "1.1.1.1").run()
    assert route.breaker.remaining() > 0 and pool.suspect(route)


def test_a_failing_save_never_fails_the_sign_in_path(capsys):
    pool = EgressPool(P1)

    def boom(row):
        raise OSError("disk full")
    pool.attach({}, boom)
    route = pool.routes[0]
    assert pool.record_blocked(route, "a@x.cz") == "account"
    pool.record_ok(route)
    failed = [r for r in _rows(capsys) if r.get("event") == "egress-state-save-failed"]
    assert failed and failed[0]["error"] == "OSError"


def test_blocked_on_a_suspect_egress_fails_over_and_signs_in(capsys):
    # A burned IP coming back from its rest costs one attempt, not a user.
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    first, second, _direct = a.pool.order("me@x.cz")
    a.pool.record_blocked(first, "x@x.cz")
    a.pool.record_blocked(first, "y@x.cz")              # tripped earlier: suspect
    first.breaker.reset()                               # ... and its rest is over
    seen = []

    def fake(email, pw, **_kw):
        seen.append(egress.current())
        if egress.current() == first.proxy:
            raise login.GarminLoginError("429", reason="blocked")
        return login.LoginResult(status="ok", tokens_json='{"t":1}')
    with patch.object(login, "start_login", side_effect=fake):
        assert isinstance(a.start_login(FORM), base.LoginOk)
    assert seen == [first.proxy, second.proxy]
    assert first.rest_s == 7200                          # next step of the ladder
    rows = _rows(capsys)
    outcomes = [(r["egress"], r["outcome"]) for r in rows if r.get("event") == "garmin-login-attempt"]
    assert outcomes == [(first.label, "blocked"), (second.label, "ok")]


def test_an_account_level_block_does_not_fail_over():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    seen, fake = _route_spy(login.GarminLoginError("429", reason="blocked"))
    with patch.object(login, "start_login", side_effect=fake):
        with pytest.raises(base.LoginError):
            a.start_login(FORM)
    assert len(seen) == 1                                # healthy egress, lone block


def test_no_failover_when_the_first_attempt_ate_the_time_budget():
    a = GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"{P1},{P2}"))
    first = a.pool.order("me@x.cz")[0]
    a.pool.record_blocked(first, "x@x.cz")
    a.pool.record_blocked(first, "y@x.cz")
    first.breaker.reset()
    seen, fake = _route_spy(login.GarminLoginError("429", reason="blocked"))
    import missingmcp.adapters.garmin as garmin
    with patch.object(login, "start_login", side_effect=fake), \
            patch.object(garmin, "_BLOCKED_FAILOVER_BUDGET_S", 0.0):   # budget already spent
        with pytest.raises(base.LoginError):
            a.start_login(FORM)
    assert seen == [first.proxy]


def test_egress_state_round_trips_through_the_store(tmp_path):
    from missingmcp import store
    conn = store.init_db(str(tmp_path / "g.db"))
    row = {"label": "10.0.0.1:3128", "egress_ip": "1.2.3.4", "rest_level": 2,
           "rest_until": 1234.5, "last_ok_at": None, "last_blocked_at": 1000.0}
    store.save_egress_state(conn, row)
    store.save_egress_state(conn, {**row, "rest_level": 3})
    assert store.load_egress_state(conn) == {"10.0.0.1:3128": {**row, "rest_level": 3}}
