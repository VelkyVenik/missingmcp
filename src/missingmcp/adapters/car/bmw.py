"""The `bmw` brand of the car connector (BMW + Mini): a client for the official
BMW CarData customer API. Spec: docs/superpowers/specs/2026-10-28-car-bmw-brand-design.md.

Contract: BMW's own OpenAPI files (portal assets, copied to
.scratch/car-connector/assets/bmw-cardata-*.json). Auth is a user-owned Client ID
(a public OAuth client, created in the MyBMW web portal) signed in with the
device authorization grant + PKCE; BMW shows a link where the user types the code. Access tokens
live ~1 h; the refresh token rotates on every refresh, so — the WHOOP rule —
only the gateway refreshes, serialized per account, and the rotated blob is
persisted before it's used. Data is read-only, through a "container" (a named
set of telematic keys) created at sign-in. The quota is ~50 requests/24 h per
Client ID, shared with the owner's other uses, so one telematic read serves both
status and location (cached in mcp.py) and nothing here retries."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
import httpx
from ... import store
from ...log import log, log_warn
from ..base import SessionExpired
from .common import CarUpstreamError

SCOPE = "authenticate_user openid cardata:api:read"
CONTAINER_NAME = "missingmcp"
DAILY_LIMIT = 50
REFRESH_MARGIN_S = 120
_CLIENT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                           r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Telematic keys the missingmcp container asks for (BMW data catalogue names; a
# key the car doesn't report is simply absent from the reply).
K_SOC = "vehicle.drivetrain.batteryManagement.header"
K_SOC_DISPLAYED = "vehicle.powertrain.electric.battery.stateOfCharge.displayed"
K_SOC_TARGET = "vehicle.powertrain.electric.battery.stateOfCharge.target"
K_RANGE = "vehicle.drivetrain.electricEngine.remainingElectricRange"
K_RANGE_KOMBI = "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange"
K_RANGE_LAST = "vehicle.drivetrain.lastRemainingRange"
K_ODOMETER = "vehicle.vehicle.travelledDistance"
K_CHARGING = "vehicle.drivetrain.electricEngine.charging.status"
K_CHARGING_LEFT = "vehicle.drivetrain.electricEngine.charging.timeRemaining"
K_PLUG = "vehicle.body.chargingPort.status"
K_CLIMATE = "vehicle.cabin.hvac.preconditioning.status.comfortState"
K_PRECONDITIONING = "vehicle.vehicle.preConditioning.activity"
K_LAT = "vehicle.cabin.infotainment.navigation.currentLocation.latitude"
K_LON = "vehicle.cabin.infotainment.navigation.currentLocation.longitude"
DOOR_KEYS = ("vehicle.cabin.door.row1.driver.isOpen",
             "vehicle.cabin.door.row1.passenger.isOpen",
             "vehicle.cabin.door.row2.driver.isOpen",
             "vehicle.cabin.door.row2.passenger.isOpen")
K_TRUNK = "vehicle.body.trunk.isOpen"
K_BONNET = "vehicle.body.hood.isOpen"
CONTAINER_KEYS = sorted({K_SOC, K_SOC_DISPLAYED, K_SOC_TARGET, K_RANGE, K_RANGE_KOMBI,
                         K_RANGE_LAST, K_ODOMETER, K_CHARGING, K_CHARGING_LEFT, K_PLUG,
                         K_CLIMATE, K_PRECONDITIONING, K_LAT, K_LON, *DOOR_KEYS, K_TRUNK,
                         K_BONNET})

DATA_AGE_NOTE = ("BMW reports what the car last sent; it never wakes the car, so values "
                 "may be minutes to hours old — see each captured_at.")


def normalize_client_id(client_id: str) -> str:
    """Lowercased + stripped Client ID; raises CarUpstreamError(invalid_key) unless
    it's the UUID the MyBMW portal shows."""
    c = (client_id or "").strip().lower()
    if not _CLIENT_ID_RE.match(c):
        raise CarUpstreamError("invalid_key")
    return c


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _oauth_error(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", ""))
    except (ValueError, AttributeError):
        return ""


def _api_error(r: httpx.Response) -> CarUpstreamError:
    if r.status_code == 401:
        return CarUpstreamError("invalid_key", 401)
    if r.status_code == 403:
        return CarUpstreamError("vin_not_covered", 403)
    if r.status_code == 404:
        return CarUpstreamError("container_missing", 404)
    if r.status_code == 429:
        return CarUpstreamError("rate_limited", 429)
    return CarUpstreamError("upstream_error", r.status_code)


def _lower(d: dict) -> dict:
    """CarData's Go-style JSON may capitalize field names: read them case-insensitively."""
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


def tokens_from(resp: dict, previous: dict | None = None) -> dict:
    """The blob's token fields from a token-endpoint reply (refresh keeps the old
    refresh token if the reply omits a new one)."""
    out = {"access_token": resp["access_token"],
           "refresh_token": resp.get("refresh_token") or (previous or {}).get("refresh_token"),
           "access_expires_at": int(time.time()) + int(resp.get("expires_in") or 3600)}
    if not out["refresh_token"]:
        raise CarUpstreamError("upstream_error", 200, "no refresh token")
    return out


class BmwClient:
    def __init__(self, config, timeout_s: float = 15.0):
        self._cfg = config
        self.auth_base = config.car_bmw_auth_base
        self.api_base = config.car_bmw_api_base
        self.timeout_s = timeout_s
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _api_headers(access_token: str) -> dict:
        return {"Authorization": f"Bearer {access_token}", "x-version": "v1",
                "Accept": "application/json"}

    # --- sign-in (blocking; callers run these under oauth._bounded) -------------

    def start_device(self, client_id: str) -> dict:
        """Request a device code. Returns the pending state's upstream half:
        device_code, code_verifier, user_code, verification_uri, expires_at."""
        verifier, challenge = _pkce()
        try:
            r = httpx.post(f"{self.auth_base}/device/code", data={
                "client_id": client_id, "response_type": "device_code", "scope": SCOPE,
                "code_challenge": challenge, "code_challenge_method": "S256"},
                timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        if r.status_code in (400, 401, 403):
            raise CarUpstreamError("invalid_key", r.status_code, _oauth_error(r))
        if r.status_code != 200:
            raise CarUpstreamError("upstream_error", r.status_code)
        d = r.json()
        uri = d.get("verification_uri_complete") or d.get("verification_uri") or ""
        if not (d.get("device_code") and d.get("user_code") and uri.startswith("https://")):
            raise CarUpstreamError("upstream_error", 200, "bad device code reply")
        return {"device_code": d["device_code"], "code_verifier": verifier,
                "user_code": d["user_code"], "verification_uri": uri,
                "expires_at": time.time() + int(d.get("expires_in") or 300)}

    def poll_token(self, client_id: str, device_code: str, code_verifier: str) -> dict:
        """One token request for a pending device code (no loop: the user clicks
        "I've approved it"). Raises pending | expired | denied | invalid_key."""
        try:
            r = httpx.post(f"{self.auth_base}/token", data={
                "grant_type": _DEVICE_GRANT, "device_code": device_code,
                "client_id": client_id, "code_verifier": code_verifier},
                timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        if r.status_code == 200:
            return r.json()
        err = _oauth_error(r)
        kind = {"authorization_pending": "pending", "slow_down": "pending",
                "expired_token": "expired", "access_denied": "denied",
                "invalid_client": "invalid_key", "unauthorized_client": "invalid_key"
                }.get(err, "upstream_error")
        raise CarUpstreamError(kind, r.status_code, err)

    def primary_vins(self, access_token: str) -> list[str]:
        try:
            r = httpx.get(f"{self.api_base}/customers/vehicles/mappings",
                          headers=self._api_headers(access_token), timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        if r.status_code != 200:
            raise _api_error(r)
        rows = r.json()
        rows = rows if isinstance(rows, list) else []
        return [str(m.get("vin", "")).upper() for m in map(_lower, rows)
                if str(m.get("mappingtype", "")).upper() == "PRIMARY"]

    def ensure_container(self, access_token: str) -> str:
        """Reuse our ACTIVE container if one exists (same name — its keys are fixed
        at creation, and the name is ours), else create it: one or two calls."""
        headers = self._api_headers(access_token)
        try:
            r = httpx.get(f"{self.api_base}/customers/containers", headers=headers,
                          timeout=self.timeout_s)
            if r.status_code != 200:
                raise _api_error(r)
            body = _lower(r.json())
            for c in map(_lower, body.get("containers") or []):
                if (c.get("name") == CONTAINER_NAME and c.get("containerid")
                        and str(c.get("state", "ACTIVE")).upper() == "ACTIVE"):
                    return str(c["containerid"])
            r = httpx.post(f"{self.api_base}/customers/containers", headers=headers,
                           json={"name": CONTAINER_NAME,
                                 "purpose": "MissingMCP car connector",
                                 "technicalDescriptors": CONTAINER_KEYS},
                           timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        if r.status_code not in (200, 201):
            raise _api_error(r)
        cid = _lower(r.json()).get("containerid")
        if not cid:
            raise CarUpstreamError("upstream_error", r.status_code, "no container id")
        return str(cid)

    # --- runtime (async, in the MCP request path) --------------------------------

    async def ensure_fresh(self, conn, account_key: str, d: dict) -> dict:
        """The blob with a usable access token. Refreshes under a per-account lock,
        re-reading the stored blob first (a queued waiter reuses the rotation the
        previous holder persisted) and persisting the rotated blob BEFORE it's
        used — the old refresh token is dead upstream the moment BMW answers."""
        if d["access_expires_at"] - time.time() > REFRESH_MARGIN_S:
            return d
        lock = self._locks.setdefault(account_key, asyncio.Lock())
        async with lock:
            raw = store.get_account_tokens(conn, "car", account_key, self._cfg.gateway_secret)
            current = json.loads(raw) if raw else None
            if not current or current.get("brand") != "bmw":
                raise SessionExpired("car account changed brand or vanished")
            # A waiter may find the account reconnected to another car while it
            # queued: that sign-in revoked this request's token, so it must never
            # proceed on (and cache under its old VIN) the new car's credentials.
            if (current.get("client_id"), current.get("vin")) != (d["client_id"], d["vin"]):
                raise SessionExpired("car account was reconnected to another car")
            if current["access_expires_at"] - time.time() > REFRESH_MARGIN_S:
                return current
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                    r = await c.post(f"{self.auth_base}/token", data={
                        "grant_type": "refresh_token",
                        "refresh_token": current["refresh_token"],
                        "client_id": current["client_id"]})
            except httpx.HTTPError as e:
                raise CarUpstreamError("upstream_error") from e
            # A reconnect (oauth._finish) doesn't take this lock and may have stored
            # new credentials — even another car — while BMW answered: it wins.
            # Re-read with no await before the write below (compare-and-set on the
            # single event loop).
            latest = self._reconnected(conn, account_key, current)
            if latest is not None:
                return latest
            if r.status_code != 200:
                err = _oauth_error(r)
                log_warn("car-refresh-failed", account=account_key, status=r.status_code,
                         reason=err or "http")
                if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
                    raise SessionExpired(f"BMW refresh rejected: {err}")
                raise CarUpstreamError("upstream_error", r.status_code, err)
            new = {**current, **tokens_from(r.json(), current)}
            store.upsert_account(conn, "car", account_key, json.dumps(new),
                                 self._cfg.gateway_secret)
            log("car-refresh-ok", account=account_key)
            return new

    def _reconnected(self, conn, account_key: str, before: dict) -> dict | None:
        """None if the stored blob is still the one we refreshed from. Otherwise a
        reconnect replaced it: the same car's new blob is returned for this request
        to use; a different car (or brand) ends it — that sign-in revoked our token."""
        raw = store.get_account_tokens(conn, "car", account_key, self._cfg.gateway_secret)
        latest = json.loads(raw) if raw else None
        if latest is not None and latest.get("refresh_token") == before["refresh_token"] \
                and latest.get("client_id") == before["client_id"] \
                and latest.get("vin") == before["vin"]:
            return None
        log("car-refresh-superseded", account=account_key)
        if latest and latest.get("brand") == "bmw" and \
                (latest.get("client_id"), latest.get("vin")) == (before["client_id"], before["vin"]):
            return latest
        raise SessionExpired("car account was reconnected during the refresh")

    async def telematics(self, d: dict) -> dict:
        """One read of the account's container: {key: {value, unit, timestamp}}."""
        url = (f"{self.api_base}/customers/vehicles/{d['vin']}/telematicData"
               f"?containerId={d['container_id']}")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.get(url, headers=self._api_headers(d["access_token"]))
        except httpx.HTTPError as e:
            raise CarUpstreamError("upstream_error") from e
        if r.status_code != 200:
            raise _api_error(r)
        data = _lower(r.json()).get("telematicdata")
        return {k: _lower(v) for k, v in data.items()} if isinstance(data, dict) else {}


# --- normalization to the brand-neutral shape (mcp.py) ---------------------------

def _num(cell: dict | None, digits: int = 1):
    v = (cell or {}).get("value")
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, digits)


def _text(cell: dict | None) -> str | None:
    v = (cell or {}).get("value")
    return str(v).lower() if v not in (None, "") else None


def _flag(cell: dict | None) -> bool | None:
    """An is-open cell: BMW sends "OPEN" / "CLOSED" (live i3, 2026-10-28); true /
    false accepted too. Anything else (null, INVALID…) is unknown."""
    v = (cell or {}).get("value")
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower() if v is not None else ""
    return {"open": True, "true": True, "closed": False, "false": False}.get(s)


def _at(*cells) -> str | None:
    stamps = [c.get("timestamp") for c in cells if c and c.get("timestamp")]
    return max(stamps) if stamps else None


def _open_closed(flag: bool | None) -> str | None:
    return None if flag is None else ("open" if flag else "closed")


def normalize_status(td: dict) -> dict:
    """Brand-neutral status (the Škoda shape where CarData has the value). Never
    carries location — that's get_vehicle_location's alone."""
    out: dict = {}
    soc = td.get(K_SOC) or td.get(K_SOC_DISPLAYED)
    if _num(soc) is not None:
        out["battery_percent"] = _num(soc)
    rng = next((td[k] for k in (K_RANGE, K_RANGE_KOMBI, K_RANGE_LAST)
                if _num(td.get(k)) is not None), None)
    if rng is not None:
        out["range_km"] = _num(rng)
    if _num(td.get(K_ODOMETER)) is not None:
        out["odometer_km"] = _num(td[K_ODOMETER])
    ch, plug = td.get(K_CHARGING), td.get(K_PLUG)
    if ch or plug:
        out["charging"] = {"state": _text(ch), "plug": _text(plug),
                           "minutes_to_full": _num(td.get(K_CHARGING_LEFT)),
                           "limit_percent": _num(td.get(K_SOC_TARGET)),
                           "captured_at": _at(ch, plug)}
    doors = [_flag(td.get(k)) for k in DOOR_KEYS if k in td]
    known = [f for f in doors if f is not None]
    if known:
        out["doors"] = "open" if any(known) else "closed"
    for name, key in (("trunk", K_TRUNK), ("bonnet", K_BONNET)):
        state = _open_closed(_flag(td.get(key)))
        if state:
            out[name] = state
    if known or "trunk" in out or "bonnet" in out:
        out["doors_captured_at"] = _at(*(td.get(k) for k in (*DOOR_KEYS, K_TRUNK, K_BONNET)))
    climate = td.get(K_CLIMATE) or td.get(K_PRECONDITIONING)
    if _text(climate):
        out["climate"] = {"state": _text(climate), "captured_at": _at(climate)}
    out["data_age_note"] = DATA_AGE_NOTE
    return out


def normalize_location(td: dict) -> dict:
    lat, lon = td.get(K_LAT), td.get(K_LON)
    out = {"parked": None, "address": None, "lat": _num(lat, 7), "lon": _num(lon, 7),
           "captured_at": _at(lat, lon)}
    if out["lat"] is None or out["lon"] is None:
        # Seen live: the keys come back with null values when the car hasn't
        # shared a position (e.g. location sharing off in the car).
        out["note"] = ("BMW has no position for this car right now — the car may not be "
                       "sharing its location.")
    return out
