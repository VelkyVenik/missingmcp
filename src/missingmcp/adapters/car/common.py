"""Pieces every car brand client shares: the upstream error type and the VIN rule."""
from __future__ import annotations
import re

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")   # ISO 3779: no I, O, Q


class CarUpstreamError(Exception):
    """One failed upstream call. kind: invalid_vin | invalid_key | vin_not_covered |
    rate_limited | car_busy | unsupported | car_timeout | upstream_error, plus the
    BMW device sign-in's pending | expired | denied.
    str(e) is safe to log: it never carries the API key or the VIN."""

    def __init__(self, kind: str, status: int = 0, problem: str = "",
                 retry_after_s: int | None = None):
        super().__init__(f"{kind} (HTTP {status} {problem})".strip())
        self.kind = kind
        self.status = status
        self.problem = problem
        self.retry_after_s = retry_after_s


def normalize_vin(vin: str) -> str:
    """Uppercased + stripped VIN; raises CarUpstreamError(invalid_vin) if malformed."""
    v = (vin or "").strip().upper()
    if not _VIN_RE.match(v):
        raise CarUpstreamError("invalid_vin")
    return v
