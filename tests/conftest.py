import json
import socket
import threading
import time
import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int) -> None:
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            return
        except OSError:
            time.sleep(0.02)


class _FakeHttpServer:
    """Shared scaffolding for the fake upstreams: single-thread HTTPServer on a
    free port with per-call recording. Subclasses provide the handler class via
    _handler() (its closure may reference self for calls/knobs)."""

    def __init__(self):
        self.port = _free_port()
        self.calls = []
        self._httpd = HTTPServer(("127.0.0.1", self.port), self._handler())
        # silence broken-pipe tracebacks when a test client times out and hangs up
        self._httpd.handle_error = lambda *a: None

    def _handler(self) -> type:
        raise NotImplementedError

    def start(self):
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._t.start()
        return self

    def stop(self):
        self._httpd.shutdown()


class FakeWorker(_FakeHttpServer):
    """A minimal HTTP server mimicking garmin-mcp's /healthz, /readyz, and /mcp.

    ``readyz_status`` knobs the readiness probe (garmin_mcp ≥ 8e809d8):
      - ``"ready"`` (default): 200 ``{"status":"ready"}``
      - ``"pending"`` / ``"failed"``: 503 with that status
      - ``None``: 404 (endpoint missing / unreachable for timeout tests)
    """

    def __init__(self):
        self.readyz_status = "ready"
        super().__init__()

    def _handler(self) -> type:
        worker = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                if self.path == "/healthz":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
                elif self.path == "/readyz":
                    status = worker.readyz_status
                    if status is None:
                        self.send_response(404); self.end_headers()
                        return
                    code = 200 if status == "ready" else 503
                    body = json.dumps({"status": status}).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404); self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                worker.calls.append(("POST", self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","result":{}}')

        return H


@pytest.fixture
def fake_worker():
    w = FakeWorker().start()
    _wait_listening(w.port)
    yield w
    w.stop()


class FakeRemoteUpstream(_FakeHttpServer):
    """A minimal HTTP server mimicking a shared hosted MCP (remote-forward
    strategy A). Records every call; tests configure the reply:
      - response_status 200 + response_mode "json" (default): JSON body + Mcp-Session-Id
      - response_mode "sse": 200 text/event-stream body written in chunks
      - response_status 401/403/500/...: JSON error body, no session id
      - response_delay: seconds to sit on the request before answering (timeout tests)
      - response_json: overrides the 2xx payload (raw string; wrapped as the SSE
        data: line in "sse" mode)
    """

    def __init__(self):
        self.response_status = 200
        self.response_mode = "json"   # "json" | "sse"
        self.response_delay = 0.0
        self.response_json = None
        self.session_id = "up-sess-9"
        super().__init__()

    def _handler(self) -> type:
        upstream = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _handle(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                upstream.calls.append((self.command, self.path, dict(self.headers), body))
                if upstream.response_delay:
                    time.sleep(upstream.response_delay)
                self.send_response(upstream.response_status)
                payload = (upstream.response_json
                           or '{"jsonrpc":"2.0","result":{"remote":true}}').encode()
                if not 200 <= upstream.response_status < 300:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"upstream says no"}')
                elif upstream.response_mode == "sse":
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Mcp-Session-Id", upstream.session_id)
                    self.end_headers()
                    # two flushes so the body arrives in chunks, like a real SSE stream
                    self.wfile.write(b"event: message\n")
                    self.wfile.flush()
                    self.wfile.write(b"data: " + payload + b"\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", upstream.session_id)
                    self.end_headers()
                    self.wfile.write(payload)

            do_POST = _handle
            do_GET = _handle
            do_DELETE = _handle
            do_PUT = _handle

        return H


@pytest.fixture
def fake_remote():
    u = FakeRemoteUpstream().start()
    _wait_listening(u.port)
    yield u
    u.stop()


class StubRemoteAdapter:
    """A complete Adapter implementing the remote-forward strategy (A) against
    FakeRemoteUpstream. The strategy stays a first-class, tested part of the
    core even with no in-tree remote adapter (rohlik graduated to its official
    MCP); this stub pins the seam's contract for the next real one."""

    name = "acme"
    display_name = "Acme"
    authorize_template = "authorize.html"   # any real template; the stub reads its own fields
    second_factor_template = ""
    landing_template = "home.html"

    class _Forward:
        def __init__(self, upstream_url):
            self.upstream_url = upstream_url

        def headers(self, blob):
            import json
            d = json.loads(blob)
            return {"x-acme-user": d["user"], "x-acme-pass": d["pass"]}

    def __init__(self, upstream_url):
        self.forward = self._Forward(upstream_url)

    def login_hint(self, form):
        return form.get("acme_user", "")

    def start_login(self, form):
        import json
        from missingmcp.adapters.base import LoginError, LoginOk, normalize_account_key
        user, password = form.get("acme_user", ""), form.get("acme_pass", "")
        if "@" not in user:
            raise LoginError("Please enter a valid email address.", reason="auth")
        return LoginOk(account_key=normalize_account_key(user),
                       blob=json.dumps({"user": user, "pass": password}))

    def resume_second_factor(self, state, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("Acme sign-in does not use a verification code")

    def verify(self, blob):
        import json
        import httpx
        from missingmcp.adapters.base import LoginError
        try:
            r = httpx.post(self.forward.upstream_url, headers=self.forward.headers(blob),
                           content=b"{}", timeout=5.0)
        except httpx.HTTPError as e:
            raise LoginError("Acme could not be reached") from e
        if r.status_code in (401, 403):
            raise LoginError("Acme sign-in failed — check your credentials.", reason="auth")
        if r.status_code >= 400:
            raise LoginError("Acme could not be reached")
        return json.loads(blob)["user"]


class FakeWhoopUpstream(_FakeHttpServer):
    """WHOOP OAuth + v2 API fake. The token endpoint mints rotating at-<n>/rt-<n>
    pairs (each minted access token becomes valid); data endpoints require a
    valid Bearer. Knobs:
      - valid_tokens: access tokens the data endpoints accept (starts empty)
      - refresh_fails: refresh grant answers refresh_fail_status with
        refresh_fail_error (default: 400 invalid_grant — the member revoked us)
      - reject_data_auth: data endpoints answer 401 regardless of token
      - data_status: force this status from data endpoints (e.g. 429, 500)
      - profile: the /v2/user/profile/basic payload
    """

    def __init__(self):
        self.valid_tokens = set()
        self.refresh_fails = False
        self.refresh_fail_status = 400
        self.refresh_fail_error = "invalid_grant"
        self.reject_data_auth = False
        self.data_status = None
        self.mint = 0
        self.profile = {"user_id": 123, "email": "User@Example.com",
                        "first_name": "Test", "last_name": "User"}
        super().__init__()

    def _next_pair(self) -> dict:
        self.mint += 1
        at = f"at-{self.mint}"
        self.valid_tokens.add(at)
        return {"access_token": at, "refresh_token": f"rt-{self.mint}",
                "expires_in": 3600, "scope": "offline read:profile", "token_type": "bearer"}

    def _handler(self) -> type:
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send_json(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                up.calls.append(("POST", self.path, dict(self.headers), body))
                if self.path != "/oauth/oauth2/token":
                    return self._send_json(404, {"error": "not_found"})
                form = parse_qs(body.decode())
                if form.get("grant_type", [""])[0] == "refresh_token" and up.refresh_fails:
                    return self._send_json(up.refresh_fail_status,
                                           {"error": up.refresh_fail_error})
                self._send_json(200, up._next_pair())

            def do_GET(self):
                up.calls.append(("GET", self.path, dict(self.headers), b""))
                if up.data_status:
                    return self._send_json(up.data_status, {"error": "forced"})
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if up.reject_data_auth or token not in up.valid_tokens:
                    return self._send_json(401, {"error": "unauthorized"})
                path = self.path.split("?")[0]
                if path == "/developer/v2/user/profile/basic":
                    return self._send_json(200, up.profile)
                if path == "/developer/v2/user/measurement/body":
                    return self._send_json(200, {"height_meter": 1.8,
                                                 "weight_kilogram": 80.0,
                                                 "max_heart_rate": 190})
                if path.startswith("/developer/v2/"):
                    # collections/by-id: echo the path so tests can assert routing
                    return self._send_json(200, {"records": [{"path": path}],
                                                 "next_token": None})
                self._send_json(404, {"error": "not_found"})

        return H


@pytest.fixture
def fake_whoop():
    u = FakeWhoopUpstream().start()
    _wait_listening(u.port)
    yield u
    u.stop()


SKODA_VIN = "TMBTEST0000000001"
SKODA_KEY = "key-1"
SKODA_PROBLEMS = "https://public.api.connect.skoda-auto.cz/problems/"


def _skoda_vehicle() -> dict:
    """Full `vehicle` object, shaped like the redacted live read (an EV: charging
    + AC only). Identifying fields hold sentinels so privacy tests can grep for them."""
    return {
        "airConditioning": {
            "state": "OFF", "airConditioningAtUnlock": True,
            "carCapturedTimestamp": "2026-10-28T08:32:56Z",
            "targetTemperature": {"value": 22.0, "unit": "CELSIUS"},
            "windowHeating": {"enabled": True, "front": "OFF", "rear": "OFF"}},
        "activeVentilation": {"state": "OFF", "carCapturedTimestamp": "2026-10-28T08:32:56Z"},
        "charging": {
            "isVehicleInSavedLocation": True, "carCapturedTimestamp": "2026-10-28T08:35:36Z",
            "settings": {"autoUnlockPlugWhenCharged": "OFF", "availableChargeModes": ["MANUAL"],
                         "batteryCareModeTargetValueInPercent": 80,
                         "chargingCareMode": "ACTIVATED", "maxChargeCurrentAc": "MAXIMUM",
                         "preferredChargeMode": "MANUAL", "targetStateOfChargeInPercent": 80},
            "status": {"battery": {"remainingCruisingRangeInMeters": 296000,
                                   "stateOfChargeInPercent": 75},
                       "chargePowerInKw": 0.0, "fullyChargedAt": "2026-10-28T08:35:36Z",
                       "plugConnectionState": "DISCONNECTED", "plugLockState": "UNLOCKED",
                       "remainingTimeToFullyChargedInMinutes": 0, "state": "CONNECT_CABLE"}},
        "licensePlate": "PLATE-SENTINEL",
        "name": "NAME-SENTINEL",
        "renderUrl": "https://render.example/RENDER-SENTINEL.png",
        "odometer": {"mileageInKm": 8279, "carCapturedTimestamp": "2026-10-28T08:42:45.085Z"},
        "operations": [{"name": n} for n in (
            "startCharging", "stopCharging", "setChargingLimit", "setChargeMode",
            "updateChargingProfile", "startAirConditioning", "stopAirConditioning")],
        "parkingPosition": {"state": "PARKED", "formattedAddress": "ADDR-SENTINEL",
                            "gpsCoordinates": {"latitude": 49.123456, "longitude": 16.654321}},
        "status": {"overall": {"doorsLocked": "YES", "locked": "YES", "doors": "CLOSED",
                               "windows": "CLOSED", "lights": "OFF",
                               "reliableLockStatus": "LOCKED"},
                   "detail": {"sunroof": "UNSUPPORTED", "trunk": "CLOSED", "bonnet": "CLOSED"},
                   "carCapturedTimestamp": "2026-10-28T08:33:00.682Z"},
    }


# include name → (vehicle key(s), error type when the car lacks it)
_SKODA_SECTIONS = {
    "info": (("name", "licensePlate", "renderUrl"), None),
    "status": (("status",), "STATUS_UNSUPPORTED"),
    "fuelStatus": (("fuelStatus",), "FUEL_STATUS_UNSUPPORTED"),
    "odometer": (("odometer",), "ODOMETER_UNSUPPORTED"),
    "parkingPosition": (("parkingPosition",), "PARKING_POSITION_UNSUPPORTED"),
    "airConditioning": (("airConditioning",), "AIR_CONDITIONING_UNSUPPORTED"),
    "auxiliaryHeating": (("auxiliaryHeating",), "AUXILIARY_HEATING_UNSUPPORTED"),
    "activeVentilation": (("activeVentilation",), "ACTIVE_VENTILATION_UNSUPPORTED"),
    "charging": (("charging",), "CHARGING_UNSUPPORTED"),
    "chargingProfiles": (("chargingProfiles",), "CHARGING_PROFILES_UNSUPPORTED"),
    "operations": (("operations",), None),
}

# action path tail → (method, operationId)
_SKODA_ACTIONS = {
    "air-conditioning/start": ("POST", "startAirConditioning"),
    "air-conditioning/stop": ("POST", "stopAirConditioning"),
    "active-ventilation/start": ("POST", "startActiveVentilation"),
    "active-ventilation/stop": ("POST", "stopActiveVentilation"),
    "charging/start": ("POST", "startCharging"),
    "charging/stop": ("POST", "stopCharging"),
    "charging/limit": ("PUT", "setChargingLimit"),
}


class FakeSkodaUpstream(_FakeHttpServer):
    """MyŠkoda Public API fake (contract 1.1.0 + live observations). Knobs:
      - keys: api key → set of VINs it covers
      - vehicle: the full vehicle object (sections returned per `include`)
      - unsupported: include names this car lacks (→ errors[] *_UNSUPPORTED)
      - expires_at / remaining / reset: X-API-Key-Expires-At and RateLimit-* values;
        each counted call (2xx, 422, 429, 5xx) decrements remaining, 401/403/404 don't
      - force: (status, problem type tail) answered by every call while set
      - delay_s: sleep before answering
    """

    def __init__(self):
        self.keys = {SKODA_KEY: {SKODA_VIN}}
        self.vehicle = _skoda_vehicle()
        self.unsupported = {"auxiliaryHeating", "activeVentilation", "fuelStatus"}
        self.expires_at = "2027-04-26T09:11:23.249Z"
        self.remaining = 20
        self.reset = 3600
        self.force = None
        self.delay_s = 0.0
        super().__init__()

    def _handler(self) -> type:
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, status, obj=None, headers=None, counted=True):
                if counted:
                    up.remaining = max(0, up.remaining - 1)
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(status)
                ctype = "application/json" if status < 400 else "application/problem+json"
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if counted:
                    self.send_header("RateLimit-Limit", "20")
                    self.send_header("RateLimit-Remaining", str(up.remaining))
                    self.send_header("RateLimit-Reset", str(up.reset))
                if 200 <= status < 300:
                    self.send_header("X-API-Key-Expires-At", up.expires_at)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _problem(self, status, tail, detail="", headers=None, counted=True):
                ptype = "about:blank" if tail == "about:blank" else SKODA_PROBLEMS + tail
                self._send(status, {"type": ptype, "title": "Problem", "status": status,
                                    "detail": detail or tail, "instance": self.path},
                           headers=headers, counted=counted)

            def _route(self, method):
                length = int(self.headers.get("content-length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                up.calls.append((method, self.path, dict(self.headers), body))
                if up.delay_s:
                    time.sleep(up.delay_s)
                path, _, query = self.path.partition("?")
                key = self.headers.get("X-API-Key", "")
                if up.force:
                    status, tail = up.force
                    extra = {"Retry-After": "1200"} if status == 429 else None
                    return self._problem(status, tail, headers=extra,
                                         counted=status not in (401, 403, 404))
                if not path.startswith("/api/v1/vehicles/"):
                    return self._problem(404, "about:blank", "No static resource", counted=False)
                if key not in up.keys:
                    return self._problem(401, "about:blank", "Missing or invalid API key.",
                                         counted=False)
                vin, _, tail = path.removeprefix("/api/v1/vehicles/").partition("/")
                if vin not in up.keys[key]:
                    return self._problem(403, "api-key-not-authorized", counted=False)
                if not tail and method == "GET":
                    return self._read(parse_qs(query).get("include", [""])[0])
                action = _SKODA_ACTIONS.get(tail)
                if action is None or action[0] != method:
                    return self._problem(404, "about:blank", "No static resource", counted=False)
                return self._act(action[1], json.loads(body) if body else {})

            def _read(self, include):
                vehicle, errors = {}, []
                for name in [i for i in include.split(",") if i]:
                    keys, err = _SKODA_SECTIONS[name]
                    if name in up.unsupported:
                        errors.append({"type": err, "description": f"{name} not supported"})
                        continue
                    for k in keys:
                        if k in up.vehicle:
                            vehicle[k] = up.vehicle[k]
                self._send(200, {"vehicle": vehicle, "errors": errors})

            def _act(self, op, body):
                v = up.vehicle
                if op == "startAirConditioning":
                    v["airConditioning"]["state"] = "HEATING"
                    if "targetTemperature" in body:
                        v["airConditioning"]["targetTemperature"] = body["targetTemperature"]
                elif op == "stopAirConditioning":
                    v["airConditioning"]["state"] = "OFF"
                elif op == "startActiveVentilation":
                    v["activeVentilation"]["state"] = "VENTILATING"
                elif op == "stopActiveVentilation":
                    v["activeVentilation"]["state"] = "OFF"
                elif op == "startCharging":
                    v["charging"]["status"]["state"] = "CHARGING"
                elif op == "stopCharging":
                    v["charging"]["status"]["state"] = "READY_FOR_CHARGING"
                elif op == "setChargingLimit":
                    v["charging"]["settings"]["targetStateOfChargeInPercent"] = \
                        body["targetStateOfChargeInPercent"]
                self._send(202)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

        return H


@pytest.fixture
def fake_skoda():
    u = FakeSkodaUpstream().start()
    _wait_listening(u.port)
    yield u
    u.stop()


BMW_CLIENT_ID = "1a2b3c4d-0000-4000-8000-00000000c0de"
BMW_VIN = "WBY00000000000042"
BMW_LAT, BMW_LON = "50.0874321", "14.4209876"      # sentinels: privacy tests grep for them


def _bmw_telematics() -> dict:
    t = "2026-10-28T08:15:00.000Z"
    cell = lambda v, unit=None: {"value": v, "unit": unit, "timestamp": t}  # noqa: E731
    return {
        "vehicle.drivetrain.batteryManagement.header": cell("74", "%"),
        "vehicle.drivetrain.electricEngine.remainingElectricRange": cell("212", "km"),
        "vehicle.vehicle.travelledDistance": cell("48210", "km"),
        "vehicle.drivetrain.electricEngine.charging.status": cell("NOCHARGING"),
        "vehicle.body.chargingPort.status": cell("DISCONNECTED"),
        "vehicle.powertrain.electric.battery.stateOfCharge.target": cell("80", "%"),
        "vehicle.cabin.door.row1.driver.isOpen": cell("CLOSED"),
        "vehicle.cabin.door.row1.passenger.isOpen": cell("CLOSED"),
        "vehicle.body.trunk.isOpen": cell("OPEN"),
        "vehicle.cabin.infotainment.navigation.currentLocation.latitude": cell(BMW_LAT),
        "vehicle.cabin.infotainment.navigation.currentLocation.longitude": cell(BMW_LON),
    }


class FakeBmwUpstream(_FakeHttpServer):
    """BMW CarData fake: the OAuth host (/device/code, /token) and the customer
    API (/customers/...) on one port. Knobs:
      - client_ids: Client IDs BMW knows
      - device_state: "pending" | "approved" | "expired" | "denied" for every device code
      - vins: the account's PRIMARY mappings
      - containers: [{"name", "containerId"}]; POST appends one
      - telematics: the container read's telematicData
      - force_api: HTTP status every /customers call answers with while set
      - refresh_fails: the refresh grant answers 400 invalid_grant
      - token_500: the next device-code token request answers 500 (seen live)
    Tokens are at-N / rt-N; a refresh invalidates the refresh token it used and
    the access tokens before it (rotation)."""

    def __init__(self):
        self.client_ids = {BMW_CLIENT_ID}
        self.device_state = "approved"
        self.vins = [BMW_VIN]
        self.containers = []
        self.telematics = _bmw_telematics()
        self.force_api = None
        self.refresh_fails = False
        self.token_500 = False
        self._n = 0
        self.valid_access, self.valid_refresh = set(), set()
        super().__init__()

    def _mint(self) -> dict:
        self._n += 1
        at, rt = f"at-{self._n}", f"rt-{self._n}"
        self.valid_access, self.valid_refresh = {at}, {rt}
        return {"access_token": at, "refresh_token": rt, "id_token": "idt", "gcid": "g-1",
                "token_type": "Bearer", "expires_in": 3599, "scope": "cardata:api:read"}

    def _handler(self) -> type:
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, status, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                length = int(self.headers.get("content-length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                up.calls.append((method, self.path, dict(self.headers), raw))
                path, _, query = self.path.partition("?")
                if path == "/device/code":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    if form.get("client_id") not in up.client_ids:
                        return self._send(400, {"error": "invalid_client"})
                    return self._send(200, {
                        "device_code": "dc-1", "user_code": "BMW-1234", "expires_in": 300,
                        "interval": 5, "verification_uri": "https://customer.example/oneid/link"})
                if path == "/token":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    grant = form.get("grant_type")
                    if grant == "refresh_token":
                        if up.refresh_fails or form.get("refresh_token") not in up.valid_refresh:
                            return self._send(400, {"error": "invalid_grant"})
                        return self._send(200, up._mint())
                    if up.token_500:
                        up.token_500 = False
                        return self._send(500)
                    state = up.device_state
                    if state == "approved":
                        return self._send(200, up._mint())
                    status, err = {"pending": (403, "authorization_pending"),
                                   "expired": (401, "expired_token"),
                                   "denied": (403, "access_denied")}[state]
                    return self._send(status, {"error": err})
                if not path.startswith("/customers/"):
                    return self._send(404)
                if self.headers.get("x-version") != "v1":
                    return self._send(403, {"exveErrorMsg": "missing x-version"})
                if up.force_api:
                    return self._send(up.force_api, {"exveErrorMsg": "forced"})
                auth = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if auth not in up.valid_access:
                    return self._send(401, {"exveErrorMsg": "unauthorized"})
                if path == "/customers/vehicles/mappings":
                    return self._send(200, [{"vin": v, "mappedSince": "2025-01-01T00:00:00Z",
                                             "mappingType": "PRIMARY"} for v in up.vins])
                if path == "/customers/containers" and method == "GET":
                    return self._send(200, {"containers": [
                        {k: v for k, v in c.items() if k != "technicalDescriptors"}
                        for c in up.containers]})
                if path == "/customers/containers" and method == "POST":
                    body = json.loads(raw)
                    c = {"name": body["name"], "purpose": body["purpose"], "state": "ACTIVE",
                         "containerId": f"C{len(up.containers) + 1:04d}",
                         "technicalDescriptors": body["technicalDescriptors"]}
                    up.containers.append(c)
                    return self._send(201, c)
                if path.startswith("/customers/vehicles/") and path.endswith("/telematicData"):
                    vin = path.split("/")[3]
                    cid = parse_qs(query).get("containerId", [""])[0]
                    if vin not in up.vins:
                        return self._send(403, {"exveErrorMsg": "vin"})
                    if cid not in {c["containerId"] for c in up.containers
                                   if c.get("state") == "ACTIVE"}:
                        return self._send(404, {"exveErrorMsg": "container"})
                    return self._send(200, {"telematicData": up.telematics})
                return self._send(404)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        return H


@pytest.fixture
def fake_bmw():
    u = FakeBmwUpstream().start()
    _wait_listening(u.port)
    yield u
    u.stop()


class StubUpstreamOAuthAdapter:
    """A complete Adapter implementing the upstream-OAuth login shape (C) with
    a canned token exchange — pins oauth.authorize_get's redirect branch and
    oauth.authorize_callback without a real upstream. Form-login methods raise:
    app.py registers no authorize POST for upstream-OAuth adapters."""

    name = "acmeauth"
    display_name = "AcmeAuth"
    authorize_template = ""
    second_factor_template = ""
    landing_template = "home.html"

    def __init__(self, fail_with: str | None = None):
        self.fail_with = fail_with
        self.forward = None          # oauth-flow tests never touch the forward
        self.callbacks = []

    def authorize_redirect_url(self, state_id: str) -> str:
        return f"https://upstream.example/auth?state={state_id}"

    async def handle_callback(self, query):
        from missingmcp.adapters.base import LoginError, LoginOk, normalize_account_key
        self.callbacks.append(dict(query))
        if self.fail_with:
            raise LoginError(self.fail_with)
        return LoginOk(account_key=normalize_account_key("Me@X.cz"),
                       blob='{"access_token":"at","refresh_token":"rt","expires_at":9999999999}')

    def login_hint(self, form):
        return ""

    def start_login(self, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("AcmeAuth signs in at the provider, not here.")

    def resume_second_factor(self, state, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("AcmeAuth signs in at the provider, not here.")

    def verify(self, blob):
        return "Acme User"


class StubLocalAdapter:
    """Adapter with the local (in-process) forward strategy: echoes the JSON-RPC
    method back and records what it was handed. Pins proxy.handle_mcp's local
    branch. Set forward.expire = True to raise SessionExpired."""

    name = "acmelocal"
    display_name = "AcmeLocal"
    authorize_template = ""
    second_factor_template = ""
    landing_template = "home.html"

    class _Forward:
        def __init__(self):
            self.handled = []
            self.expire = False

        async def handle(self, conn, account_key, blob, body):
            import json
            from missingmcp.adapters.base import SessionExpired
            if self.expire:
                raise SessionExpired("stale")
            self.handled.append((account_key, blob, body))
            payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "result": {"echo": json.loads(body)["method"]}}).encode()
            return 200, {"Content-Type": "application/json"}, payload

    def __init__(self):
        self.forward = self._Forward()

    def login_hint(self, form):
        return ""

    def start_login(self, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("AcmeLocal signs in at the provider, not here.")

    def resume_second_factor(self, state, form):
        from missingmcp.adapters.base import LoginError
        raise LoginError("AcmeLocal signs in at the provider, not here.")

    def verify(self, blob):
        return "Acme Local"
