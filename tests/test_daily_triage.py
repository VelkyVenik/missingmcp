"""Daily triage — the pure classification/gating/rendering logic and the Claude
call's failure handling (PostHog/Slack I/O is exercised via --dry-run)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import daily_triage as dt  # noqa: E402


def _row(**fields):
    return fields


# --- classification ----------------------------------------------------------

def test_classify_signature_table():
    rows = [
        _row(event="worker-forward-auth-stale", account="a@x.cz"),
        _row(event="worker-log", account="b@x.cz",
             line="ERROR: OAuth tokens not found and no interactive terminal available."),
        _row(event="worker-log", account="c@x.cz",
             line="[08/19/26 10:05:22] ERROR    API call failed for path"),
        _row(event="authorize-csrf-invalid"),
        _row(event="worker-start-failed", account="d@x.cz", error="rc=1"),
        _row(event="mcp-forward-error", error="ConnectError", account="e@x.cz"),
        _row(event="stdlib-log", message="Login failed: All login strategies exhausted"),
        _row(event="some-brand-new-event", account="f@x.cz"),
    ]
    c = dt.classify(rows)
    assert c["credential-expiry"]["count"] == 2
    assert c["garmin-upstream"]["count"] == 2       # worker API fail + portal login
    assert c["auth-flow-noise"]["count"] == 1
    assert c["worker-fault"]["count"] == 1
    assert c["forward-error"]["count"] == 1
    assert c["unclassified"]["count"] == 1


def test_classify_folds_traceback_continuations():
    rows = [
        _row(event="worker-log", account="a@x.cz",
             line="[08/19/26 10:05:22] ERROR    API call failed for path"),
        _row(event="worker-log", account="a@x.cz",
             line="╭─ Traceback (most recent call la─╮"),
    ]
    c = dt.classify(rows)
    assert c["garmin-upstream"]["count"] == 1       # the decoration row folded away
    assert len(c) == 1


def test_classify_masks_emails_in_samples():
    rows = [_row(event="mcp-forward-error", account="jane.doe@example.com")]
    c = dt.classify(rows)
    sample = c["forward-error"]["samples"][0]
    assert "jane.doe@example.com" not in sample
    assert "jan***" in sample


def test_classify_survives_non_dict_rows():
    c = dt.classify(["not a dict at all"])
    assert c["unclassified"]["count"] == 1


# --- the gate: what wakes the analyst ---------------------------------------

def test_routine_classes_alone_do_not_open_the_analysis():
    c = dt.classify([_row(event="worker-forward-auth-stale"),
                     _row(event="authorize-csrf-invalid")])
    assert dt.actionable_classes(c) == []


def test_garmin_upstream_is_routine_below_burst_threshold():
    rows = [_row(event="worker-log", account="a@x.cz",
                 line="ERROR    API call failed for path")] * 10
    assert dt.actionable_classes(dt.classify(rows)) == []
    burst = rows * 20                               # 200 > threshold 150
    assert dt.actionable_classes(dt.classify(burst)) == ["garmin-upstream"]


def test_faults_and_unknowns_open_the_analysis():
    c = dt.classify([_row(event="worker-start-failed", account="a@x.cz"),
                     _row(event="never-seen-before")])
    assert dt.actionable_classes(c) == ["unclassified", "worker-fault"]


# --- Claude call: parsing + failure handling ---------------------------------

class _Resp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_claude_analyze_joins_text_blocks_and_skips_thinking(monkeypatch):
    monkeypatch.setattr(dt.httpx, "post", lambda *a, **k: _Resp(200, {
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "line one\n"},
                    {"type": "text", "text": "line two"}],
    }))
    assert dt.claude_analyze("k", "m", {}) == "line one\nline two"


def test_claude_analyze_refusal_degrades_to_none(monkeypatch):
    monkeypatch.setattr(dt.httpx, "post", lambda *a, **k: _Resp(200, {
        "stop_reason": "refusal", "content": []}))
    assert dt.claude_analyze("k", "m", {}) is None


def test_claude_analyze_retries_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(429, {}, headers={"retry-after": "0"})
        return _Resp(200, {"stop_reason": "end_turn",
                           "content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(dt.httpx, "post", fake_post)
    monkeypatch.setattr(dt.time, "sleep", lambda s: None)
    assert dt.claude_analyze("k", "m", {}) == "ok"
    assert len(calls) == 2


def test_claude_analyze_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(dt.httpx, "post", lambda *a, **k: _Resp(529, {}))
    monkeypatch.setattr(dt.time, "sleep", lambda s: None)
    assert dt.claude_analyze("k", "m", {}) is None


def test_claude_analyze_hard_4xx_does_not_retry(monkeypatch):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        return _Resp(400, {"error": {"type": "invalid_request_error"}})

    monkeypatch.setattr(dt.httpx, "post", fake_post)
    assert dt.claude_analyze("k", "m", {}) is None
    assert len(calls) == 1


# --- rendering ----------------------------------------------------------------

def _agg(classes=None, actionable=None):
    return {"window_hours": 24, "classes": classes or {},
            "actionable": actionable or [],
            "teardown_total": 120, "teardown_post_side": 3,
            "error_row_count": 42}


def test_render_healthy_day_is_one_line():
    text = dt.render(_agg(), None)
    assert "klid" in text and "\n" not in text
    assert "42 error řádků" in text and "120" in text


def test_render_analysis_day_carries_claude_text():
    text = dt.render(_agg(classes={"worker-fault": {"count": 2, "samples": []}},
                          actionable=["worker-fault"]), "THE ANALYSIS")
    assert "THE ANALYSIS" in text and "je co řešit" in text


def test_render_degrades_to_deterministic_table_when_analysis_missing():
    text = dt.render(_agg(classes={"worker-fault": {"count": 2, "samples": []},
                                   "credential-expiry": {"count": 5, "samples": []}},
                          actionable=["worker-fault"]), None)
    assert "analýza nedostupná" in text
    assert "worker-fault: 2" in text and "credential-expiry: 5" in text


# --- backend dispatch ----------------------------------------------------------

def test_analyze_prefers_subscription_backend(monkeypatch):
    calls = {}

    class _Proc:
        returncode = 0
        stdout = "SUBSCRIPTION ANALYSIS\n"
        stderr = ""

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _Proc()

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-used")
    monkeypatch.setattr(dt.subprocess, "run", fake_run)
    monkeypatch.setattr(dt.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert dt.analyze({"x": 1}) == "SUBSCRIPTION ANALYSIS"
    assert calls["cmd"][0] == "claude" and "-p" in calls["cmd"]
    tools = calls["cmd"][calls["cmd"].index("--allowedTools") + 1]
    assert "Bash" not in tools and "Write" not in tools    # read-only grounding


def test_analyze_subscription_failure_degrades_to_none(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(dt.subprocess, "run", lambda *a, **k: _Proc())
    assert dt.analyze({}) is None


def test_analyze_without_any_backend_is_none(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert dt.analyze({}) is None


# --- deployment window selection ------------------------------------------------

def test_pick_deployments_covers_the_whole_window():
    # Railway logs are per-deployment: the window must include every deployment
    # created inside it PLUS the newest one from before (live at window start).
    deps = [
        {"id": "newest", "createdAt": "2026-08-19T20:28:06.998Z"},
        {"id": "midday", "createdAt": "2026-08-19T10:00:00.000Z"},
        {"id": "before", "createdAt": "2026-08-18T09:00:00.000Z"},
        {"id": "ancient", "createdAt": "2026-08-10T09:00:00.000Z"},
    ]
    picked = dt.pick_deployments(deps, "2026-08-18T21:00:00Z")
    assert picked == ["newest", "midday", "before"]     # ancient excluded


def test_pick_deployments_no_deploy_inside_window():
    deps = [{"id": "old", "createdAt": "2026-08-10T09:00:00.000Z"}]
    assert dt.pick_deployments(deps, "2026-08-18T21:00:00Z") == ["old"]
