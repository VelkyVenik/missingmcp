from __future__ import annotations
import json
import logging
import os
import sys
import time
import traceback
from typing import Any, TextIO

_file: TextIO | None = None

# Optional tee of every emitted record (telemetry.py ships the log stream to
# PostHog through this). The sink sees the exact record dict that went to
# stdout, AFTER it was written — so a broken sink can never lose a log line.
_sink = None

_VALID_LEVELS = {"debug", "info", "warning", "error", "critical"}


def set_sink(fn) -> None:
    """Install (or with None, remove) the record tee. The sink must never
    raise into callers; _emit guards it anyway."""
    global _sink
    _sink = fn


def resolve_log_level() -> str:
    """stdlib/uvicorn log level name from GATEWAY_LOG_LEVEL (default 'info').
    Use 'debug' to capture garminconnect / urllib3 / uvicorn internals."""
    lvl = os.environ.get("GATEWAY_LOG_LEVEL", "info").strip().lower()
    return lvl if lvl in _VALID_LEVELS else "info"


_STDLIB_LEVELS = {  # stdlib levelname -> our structured schema's level values
    "DEBUG": "debug", "INFO": "info", "WARNING": "warn",
    "ERROR": "error", "CRITICAL": "critical",
}


class _StructuredHandler(logging.Handler):
    """Bridges stdlib logging (uvicorn, garminconnect, urllib3, warnings) into
    the structured JSON stream on STDOUT. Without it those loggers fall back to
    logging.lastResort on STDERR, which Railway classifies as error-severity —
    uvicorn's plain 'INFO: Started server process' lines showed up as errors."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields: dict[str, Any] = {"logger": record.name,
                                      "message": record.getMessage()}
            if record.exc_info:
                fields["traceback"] = "".join(
                    traceback.format_exception(*record.exc_info))
            _emit(_STDLIB_LEVELS.get(record.levelname, "info"), "stdlib-log", fields)
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            pass


def setup_logging(path: str | None = None) -> None:
    """Route ALL logging to structured JSON on stdout: our own log()/log_error()
    plus stdlib/uvicorn/warnings via _StructuredHandler (level from
    GATEWAY_LOG_LEVEL). When GATEWAY_LOG_FILE (or `path`) is set, also tee both
    streams to that file."""
    global _file
    path = path or os.environ.get("GATEWAY_LOG_FILE")
    level = getattr(logging, resolve_log_level().upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):        # idempotent re-setup (tests, reloads)
        if isinstance(h, _StructuredHandler):
            root.removeHandler(h)
    root.addHandler(_StructuredHandler())
    logging.captureWarnings(True)
    if path:
        # Tee JSON once: _emit writes to _file, and _StructuredHandler bridges
        # stdlib/uvicorn records through _emit too — so a plain-text FileHandler
        # would double-write every bridged record and open the file twice.
        _file = open(path, "a", encoding="utf-8", buffering=1)
    if path or level != logging.INFO:
        _emit("info", "logging-initialised",
              {"path": path or "", "log_level": logging.getLevelName(level)})


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    record = {"ts": time.strftime("%H:%M:%S"), "level": level, "event": event}
    for k, v in fields.items():
        if k in ("ts", "level", "event"):   # never let a field clobber the envelope
            k = f"field_{k}"
        record[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    line = json.dumps(record) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if _file is not None:
        _file.write(line)
        _file.flush()
    if _sink is not None:
        try:
            _sink(record)
        except Exception:  # noqa: BLE001 - the tee must never break logging
            pass


def log(event: str, **fields: Any) -> None:
    _emit("info", event, fields)


def log_warn(event: str, **fields: Any) -> None:
    _emit("warn", event, fields)


def log_error(event: str, **fields: Any) -> None:
    _emit("error", event, fields)


def log_exc(event: str, exc: BaseException | None = None, **fields: Any) -> None:
    """Like log_error but attaches a full traceback (current exception if exc
    is None)."""
    if exc is None:
        fields["traceback"] = traceback.format_exc()
    else:
        fields["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    _emit("error", event, fields)
