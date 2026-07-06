from __future__ import annotations
import json
import logging
import os
import sys
import time
import traceback
from typing import Any, TextIO

_file: TextIO | None = None

_VALID_LEVELS = {"debug", "info", "warning", "error", "critical"}


def resolve_log_level() -> str:
    """stdlib/uvicorn log level name from GATEWAY_LOG_LEVEL (default 'info').
    Use 'debug' to capture garminconnect / urllib3 / uvicorn internals."""
    lvl = os.environ.get("GATEWAY_LOG_LEVEL", "info").strip().lower()
    return lvl if lvl in _VALID_LEVELS else "info"


def setup_logging(path: str | None = None) -> None:
    """Set the stdlib logging level from GATEWAY_LOG_LEVEL and, when
    GATEWAY_LOG_FILE (or `path`) is set, also tee structured + stdlib logs to
    that file. Structured log()/log_error() always go to stdout regardless."""
    global _file
    path = path or os.environ.get("GATEWAY_LOG_FILE")
    level = getattr(logging, resolve_log_level().upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if path:
        _file = open(path, "a", encoding="utf-8", buffering=1)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    if path or level != logging.INFO:
        _emit("info", "logging-initialised",
              {"path": path or "", "level": logging.getLevelName(level)})


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    record = {"ts": time.strftime("%H:%M:%S"), "level": level, "event": event}
    for k, v in fields.items():
        record[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    line = json.dumps(record) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if _file is not None:
        _file.write(line)
        _file.flush()


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
