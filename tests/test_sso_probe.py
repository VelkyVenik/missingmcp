import json
import time
from missingmcp.adapters.garmin.probe import SsoProbe
from missingmcp.config import load_config


def _rows(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]


def test_probe_is_off_by_default():
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x"})
    assert cfg.sso_probe_interval == 0
    assert SsoProbe(cfg.sso_probe_interval).enabled is False


def test_probe_logs_status_and_reschedules(capsys):
    p = SsoProbe(600, fetch=lambda: 429)
    assert p.due()                      # first probe fires immediately
    p.run()
    row = [r for r in _rows(capsys) if r.get("event") == "sso-probe"][0]
    assert row["status"] == 429 and "ms" in row
    assert not p.due()                  # rescheduled a full interval ahead
    assert p.due(now=time.monotonic() + 601)


def test_probe_never_raises_and_logs_the_error(capsys):
    # The lifespan loop calls run() via to_thread — a failing GET (TLS, DNS,
    # timeout) must become a data point, never an exception in the loop.
    def boom():
        raise RuntimeError("tls exploded")
    p = SsoProbe(600, fetch=boom)
    p.run()
    row = [r for r in _rows(capsys) if r.get("event") == "sso-probe"][0]
    assert row["status"] is None and row["error"] == "RuntimeError"


def test_probe_floors_a_mistyped_interval():
    # SSO_PROBE_INTERVAL=5 must not turn into hammering Garmin every 5 s.
    assert SsoProbe(5, fetch=lambda: 200).interval == 60
    assert SsoProbe(1800, fetch=lambda: 200).interval == 1800
    assert SsoProbe(0).enabled is False and SsoProbe(0).interval == 0
