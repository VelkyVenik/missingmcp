"""The `skoda` brand of the car connector: a client for the official MyŠkoda
Public API (contract 1.1.0, https://public.api.connect.skoda-auto.cz/docs).

Auth is a user-owned `X-API-Key` (created in the MyŠkoda app, ~6-month life),
bound to the VINs picked at creation. The budget is 20 requests/hour per VIN,
shared by every key on that car, so this module never retries: each call is
exactly one upstream request. Errors are application/problem+json, told apart
by the `type` tail (two different 429s share the status).

A future brand implements the same surface: probe_sync / read / action raising
CarUpstreamError, plus normalize_status / normalize_location."""
from __future__ import annotations
import re
import time
from dataclasses import dataclass
import httpx

PROBLEM_PREFIX = "/problems/"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")   # ISO 3779: no I, O, Q

# operationId → (HTTP method, path tail under /api/v1/vehicles/{vin})
ACTIONS = {
    "startAirConditioning": ("POST", "air-conditioning/start"),
    "stopAirConditioning": ("POST", "air-conditioning/stop"),
    "startActiveVentilation": ("POST", "active-ventilation/start"),
    "stopActiveVentilation": ("POST", "active-ventilation/stop"),
    "startCharging": ("POST", "charging/start"),
    "stopCharging": ("POST", "charging/stop"),
    "setChargingLimit": ("PUT", "charging/limit"),
}


class CarUpstreamError(Exception):
    """One failed upstream call. kind: invalid_vin | invalid_key | vin_not_covered |
    rate_limited | car_busy | unsupported | car_timeout | upstream_error.
    str(e) is safe to log: it never carries the API key or the VIN."""

    def __init__(self, kind: str, status: int = 0, problem: str = "",
                 retry_after_s: int | None = None):
        super().__init__(f"{kind} (HTTP {status} {problem})".strip())
        self.kind = kind
        self.status = status
        self.problem = problem
        self.retry_after_s = retry_after_s


@dataclass
class Budget:
    remaining: int | None
    reset_s: int | None
    at: float                      # time.monotonic() when observed


@dataclass
class Reply:
    data: dict | None              # parsed JSON for reads, None for 202 actions
    budget: Budget | None
    expires_at: str | None


def normalize_vin(vin: str) -> str:
    """Uppercased + stripped VIN; raises CarUpstreamError(invalid_vin) if malformed."""
    v = (vin or "").strip().upper()
    if not _VIN_RE.match(v):
        raise CarUpstreamError("invalid_vin")
    return v


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _budget(r: httpx.Response) -> Budget | None:
    remaining = _int(r.headers.get("RateLimit-Remaining"))
    if remaining is None:
        return None
    return Budget(remaining, _int(r.headers.get("RateLimit-Reset")), time.monotonic())


def _problem_tail(r: httpx.Response) -> str:
    try:
        ptype = r.json().get("type", "")
    except ValueError:
        return ""
    return ptype.rsplit(PROBLEM_PREFIX, 1)[-1] if PROBLEM_PREFIX in ptype else ptype


def _error(r: httpx.Response) -> CarUpstreamError:
    s, tail = r.status_code, _problem_tail(r)
    if s == 401:
        kind = "invalid_key"
    elif s == 403:
        kind = "vin_not_covered"
    elif s == 422:
        kind = "unsupported"
    elif s == 429:
        kind = "car_busy" if tail == "vehicle-not-accepting-requests" else "rate_limited"
    elif s == 504:
        kind = "car_timeout"
    else:
        kind = "upstream_error"
    return CarUpstreamError(kind, s, tail, _int(r.headers.get("Retry-After")))


def _reply(r: httpx.Response) -> Reply:
    if r.status_code >= 400:
        raise _error(r)
    data = r.json() if r.status_code == 200 and r.content else None
    return Reply(data, _budget(r), r.headers.get("X-API-Key-Expires-At"))


class SkodaClient:
    def __init__(self, base_url: str, timeout_s: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _url(self, vin: str, tail: str = "") -> str:
        return f"{self.base_url}/api/v1/vehicles/{normalize_vin(vin)}" + (f"/{tail}" if tail else "")

    @staticmethod
    def _headers(api_key: str) -> dict:
        return {"X-API-Key": api_key, "Accept": "application/json"}

    def probe_sync(self, api_key: str, vin: str) -> Reply:
        """Sign-in check (blocking; callers run it under oauth._bounded): one GET
        for the car's operations, which also proves the key covers the VIN."""
        url = self._url(vin)
        try:
            r = httpx.get(url, params={"include": "operations"},
                          headers=self._headers(api_key), timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        return _reply(r)

    async def read(self, api_key: str, vin: str, includes: list[str]) -> Reply:
        url = self._url(vin)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                # literal commas: the contract documents ?include=a,b
                r = await c.get(f"{url}?include={','.join(includes)}",
                                headers=self._headers(api_key))
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        return _reply(r)

    async def action(self, api_key: str, vin: str, op: str, body: dict | None) -> Reply:
        if op not in ACTIONS:
            raise ValueError(f"unknown operation: {op}")
        method, tail = ACTIONS[op]
        url = self._url(vin, tail)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.request(method, url, headers=self._headers(api_key),
                                    json=body)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        return _reply(r)
