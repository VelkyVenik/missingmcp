import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from garminconnect import client as gc_client

from missingmcp.adapters.garmin import GarminAdapter, egress
from missingmcp.adapters.garmin.probe import SsoProbe
from missingmcp.config import load_config

PROXY = "http://user:s3cret@proxy.example:3128"


@pytest.fixture(autouse=True)
def _reset_egress():
    yield
    egress.configure(None)


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
    GarminAdapter(cfg)
    assert egress.current() is None
    assert gc_client.requests.Session().proxies == {}


def test_describe_never_leaks_credentials():
    assert egress.describe(PROXY) == "proxy.example:3128"
    assert "s3cret" not in egress.describe(PROXY)
    assert egress.describe("") is None


def test_adapter_configures_proxy_and_logs_host_only(capsys):
    GarminAdapter(_cfg(GARMIN_SSO_PROXY=f"  {PROXY} "))
    assert egress.current() == PROXY
    out = capsys.readouterr().out
    assert "s3cret" not in out
    row = [json.loads(line) for line in out.splitlines() if '"garmin-sso-proxy"' in line][0]
    assert row["proxy"] == "proxy.example:3128"


def test_garminconnect_sessions_get_the_proxy():
    egress.configure(PROXY)
    s = gc_client.requests.Session()
    assert s.proxies == {"http": PROXY, "https": PROXY}
    # The wrappers stay drop-in for the rest of the module API garminconnect uses.
    assert gc_client.requests.adapters.HTTPAdapter is not None
    egress.configure(None)
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
    egress.configure(PROXY)
    w.Session(impersonate="chrome")
    w.post("https://diauth.garmin.com/x", impersonate="chrome")
    assert calls == [
        ("Session", {"impersonate": "chrome"}),
        ("Session", {"impersonate": "chrome", "proxy": PROXY}),
        ("post", {"impersonate": "chrome", "proxy": PROXY}),
    ]


def test_requests_traffic_really_goes_through_the_proxy(recording_proxy):
    url, seen = recording_proxy
    egress.configure(url)
    r = gc_client.requests.Session().get("http://sso.garmin.invalid/portal", timeout=5)
    assert r.status_code == 200
    assert seen == ["http://sso.garmin.invalid/portal"]


def test_cffi_traffic_really_goes_through_the_proxy(recording_proxy):
    url, seen = recording_proxy
    egress.configure(url)
    s = gc_client.cffi_requests.Session(impersonate="chrome")
    r = s.get("http://sso.garmin.invalid/portal", timeout=5)
    assert r.status_code == 200
    assert seen == ["http://sso.garmin.invalid/portal"]


def test_probe_reports_which_egress_it_measured(capsys):
    SsoProbe(600, fetch=lambda: 200).run()
    egress.configure(PROXY)
    SsoProbe(600, fetch=lambda: 429).run()
    rows = [r for r in _rows(capsys) if r.get("event") == "sso-probe"]
    assert [(r["status"], r["via"]) for r in rows] == [(200, "direct"), (429, "proxy")]
