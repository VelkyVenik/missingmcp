"""Hourly health digest — the pure log-processing + verdict logic (the I/O
functions that hit Railway/Slack are exercised manually via --dry-run)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import hourly_digest as hd  # noqa: E402


def _entry(level="info", event=None, status=None, account=None, message="", attrs=True):
    a = []
    if attrs:
        if event:
            a.append({"key": "event", "value": event})
        if status is not None:
            a.append({"key": "status", "value": str(status)})
        if account:
            a.append({"key": "account", "value": account})
    return {"timestamp": "2026-07-18T12:00:00Z", "severity": level if attrs else None,
            "message": message, "attributes": a}


def test_parse_row_structured():
    p = hd.parse_row(_entry(level="info", event="mcp-response", status=200, account="a@x"))
    assert p == {"level": "info", "event": "mcp-response", "account": "a@x", "status": 200}


def test_parse_row_normalizes_severity():
    assert hd.parse_row(_entry(level="err", event="x"))["level"] == "error"
    assert hd.parse_row(_entry(level="warn", event="x"))["level"] == "warn"


def test_parse_row_decodes_json_encoded_attribute_values():
    # Railway JSON-encodes string attr values (event -> '"mcp-response"'), numbers
    # bare (status -> "200"). parse_row must decode so events/accounts are clean
    # and status is an int — otherwise event names keep their quotes and the
    # SELF_HEAL_EVENTS exclusion silently fails.
    entry = {"timestamp": "t", "severity": "info", "message": "",
             "attributes": [{"key": "event", "value": '"mcp-response"'},
                            {"key": "status", "value": "200"},
                            {"key": "account", "value": '"me@x"'}]}
    p = hd.parse_row(entry)
    assert p == {"level": "info", "event": "mcp-response", "account": "me@x", "status": 200}


def test_parse_row_self_heal_event_decoded_is_excluded():
    # A JSON-encoded self-heal event must still be recognized after decoding.
    rows = [{"timestamp": "t", "severity": "error", "message": "",
             "attributes": [{"key": "event", "value": '"worker-start-failed"'}]}]
    assert hd.summarize(rows)["err_rows"] == 0 and hd.summarize(rows)["reauth"] == 1


def test_railway_graphql_prefers_project_token_then_falls_back_to_bearer(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(headers)
        if "Project-Access-Token" in headers:
            return _Resp({"errors": [{"message": "Not Authorized"}]})
        return _Resp({"data": {"ok": 1}})

    monkeypatch.setattr(hd.httpx, "post", fake_post)
    assert hd.railway_graphql("tok", "q", {}) == {"ok": 1}
    assert "Project-Access-Token" in calls[0] and "Authorization" in calls[1]


def test_railway_graphql_does_not_retry_on_non_auth_error(monkeypatch):
    import pytest
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"errors": [{"message": "Field 'foo' doesn't exist"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(headers)
        return _Resp()

    monkeypatch.setattr(hd.httpx, "post", fake_post)
    with pytest.raises(RuntimeError):
        hd.railway_graphql("tok", "q", {})
    assert len(calls) == 1   # a real query error is not retried with the other header


def test_parse_row_json_message_fallback():
    # attributes empty → parse the raw JSON message (log.py emits no `message` key)
    raw = '{"level":"error","event":"local-forward-error","account":"z@x","status":502}'
    p = hd.parse_row({"timestamp": "t", "severity": None, "message": raw, "attributes": []})
    assert p["level"] == "error" and p["event"] == "local-forward-error"
    assert p["status"] == 502 and p["account"] == "z@x"


def test_parse_row_fallback_when_platform_attrs_present_but_no_event():
    # Railway injects platform attributes (source/podId) but not our fields, and
    # dumps our JSON into `message` → fallback must still run (not blocked by
    # non-empty attrs) so the row isn't invisible to the digest.
    raw = '{"level":"error","event":"local-forward-error","status":502,"account":"z@x"}'
    entry = {"timestamp": "t", "severity": None, "message": raw,
             "attributes": [{"key": "source", "value": "railway"},
                            {"key": "podId", "value": "abc"}]}
    p = hd.parse_row(entry)
    assert p["event"] == "local-forward-error" and p["status"] == 502 and p["level"] == "error"


def test_summarize_does_not_double_count_a_row():
    # A single row carrying BOTH a 5xx status and error level counts once.
    rows = [_entry(level="error", event="mcp-forward-error", status=502, account="a@x")]
    s = hd.summarize(rows)
    assert s["problems"] == 1


def _mixed():
    return [
        _entry(event="mcp-request"), _entry(event="mcp-request"),
        _entry(event="mcp-response", status=200, account="a@x"),
        _entry(event="mcp-response", status=502, account="a@x"),   # a 5xx
        _entry(level="error", event="worker-start-failed", account="b@x"),  # self-heal, not anomaly
        _entry(level="error", event="local-forward-error", account="b@x"),  # real error
    ]


def test_summarize_counts_and_excludes_self_heal():
    s = hd.summarize(_mixed())
    assert s["requests"] == 2
    assert s["http_5xx"] == 1
    assert s["err_rows"] == 1          # local-forward-error only; worker-start-failed excluded
    assert s["reauth"] == 1            # worker-start-failed
    assert s["critical"] == 0
    assert s["problems"] == 2          # 5xx + err_rows
    assert s["accounts"] == 2


def test_verdict_healthy_is_silent():
    s = hd.summarize([_entry(event="mcp-request"),
                      _entry(event="mcp-response", status=200, account="a@x")])
    v = hd.verdict(s, probe_ok=True, is_heartbeat=False, anomaly_min=3)
    assert v == {"should_post": False, "loud": False, "minor": False, "heartbeat": False}


def test_verdict_heartbeat_posts_quiet():
    s = hd.summarize([_entry(event="mcp-response", status=200, account="a@x")])
    v = hd.verdict(s, probe_ok=True, is_heartbeat=True, anomaly_min=3)
    assert v["should_post"] and v["heartbeat"] and not v["loud"] and not v["minor"]


def test_verdict_minor_below_threshold():
    s = hd.summarize(_mixed())                       # problems == 2
    v = hd.verdict(s, probe_ok=True, is_heartbeat=False, anomaly_min=3)
    assert v["minor"] and not v["loud"] and v["should_post"]


def test_verdict_loud_at_threshold():
    rows = _mixed() + [_entry(event="mcp-response", status=500, account="c@x")]  # problems == 3
    v = hd.verdict(hd.summarize(rows), probe_ok=True, is_heartbeat=False, anomaly_min=3)
    assert v["loud"] and v["should_post"]


def test_verdict_probe_failure_is_loud_even_when_clean():
    s = hd.summarize([_entry(event="mcp-request")])
    v = hd.verdict(s, probe_ok=False, is_heartbeat=False, anomaly_min=3)
    assert v["loud"] and v["should_post"]


def test_verdict_self_heal_alone_is_not_an_anomaly():
    rows = [_entry(level="error", event="worker-start-failed", account=f"{i}@x")
            for i in range(5)]
    v = hd.verdict(hd.summarize(rows), probe_ok=True, is_heartbeat=False, anomaly_min=3)
    assert not v["loud"] and not v["minor"] and not v["should_post"]


def test_render_down_and_loud_ping_here():
    s = hd.summarize([_entry(event="mcp-request")])
    down = hd.render(s, probe_ok=False,
                     v={"loud": True, "minor": False, "heartbeat": False},
                     window_min=60, gateway_url="https://missingmcp.com")
    assert "DOWN" in down and "<!here>" in down
    loud = hd.render(hd.summarize(_mixed()), probe_ok=True,
                     v={"loud": True, "minor": False, "heartbeat": False},
                     window_min=60, gateway_url="https://missingmcp.com")
    assert "<!here>" in loud
    healthy = hd.render(s, probe_ok=True,
                        v={"loud": False, "minor": False, "heartbeat": True},
                        window_min=60, gateway_url="https://missingmcp.com")
    assert "healthy" in healthy and "<!here>" not in healthy
