"""The structured-logging contract: everything must reach STDOUT as JSON with a
proper level attribute — Railway classifies plain STDERR output as
error-severity (uvicorn's default handlers did exactly that in production)."""
import io
import json
import logging
import sys
from missingmcp import log as mlog
from missingmcp.workers import _pump_worker_output


def _capture(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]


def _setup_clean():
    mlog.setup_logging(path=None)


def test_stdlib_records_become_structured_stdout_json(capsys):
    _setup_clean()
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    logging.getLogger("garminconnect").warning("odd response")
    events = _capture(capsys)
    uvi = next(e for e in events if e.get("logger") == "uvicorn.error")
    assert uvi["event"] == "stdlib-log" and uvi["level"] == "info"
    assert uvi["message"] == "Started server process [1]"
    warn = next(e for e in events if e.get("logger") == "garminconnect")
    assert warn["level"] == "warn"


def test_stdlib_exceptions_carry_traceback(capsys):
    _setup_clean()
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("starlette").exception("handler failed")
    events = _capture(capsys)
    e = next(e for e in events if e.get("logger") == "starlette")
    assert e["level"] == "error" and "ValueError: boom" in e["traceback"]


def test_setup_logging_is_idempotent(capsys):
    _setup_clean()
    _setup_clean()   # re-setup must not duplicate handlers → one line per record
    logging.getLogger("dup-check").info("once")
    events = [e for e in _capture(capsys) if e.get("logger") == "dup-check"]
    assert len(events) == 1


def test_worker_pump_emits_structured_lines_with_severity(capsys):
    lines = io.StringIO(
        "INFO:     Uvicorn running on http://127.0.0.1:9000\n"
        "\n"
        "ERROR:    something broke\n"
        "plain progress line\n"
    )
    _pump_worker_output(lines, "me@x.cz")
    events = _capture(capsys)
    assert [e["event"] for e in events] == ["worker-log"] * 3   # blank line dropped
    assert all(e["account"] == "me@x.cz" for e in events)
    assert events[0]["level"] == "info"
    assert events[1]["level"] == "error"      # ERROR heuristic elevates severity
    assert events[2]["level"] == "info"
