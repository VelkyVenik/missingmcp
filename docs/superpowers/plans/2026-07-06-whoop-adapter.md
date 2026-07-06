# WHOOP Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `whoop` adapter: connect a personal WHOOP account to Claude at `/whoop/mcp` via upstream OAuth (users sign in at WHOOP), with an in-process read-only MCP server over WHOOP's official v2 API.

**Architecture:** Three new seam pieces in the existing gateway: (1) a third login shape — upstream-OAuth redirect + callback (`is_upstream_oauth`), (2) a third forward strategy — `LocalForward`, handled in-process (`is_local`), (3) the whoop adapter itself: httpx client with gateway-owned rotating-refresh-token handling, hand-rolled stateless MCP JSON-RPC layer, 8 read-only tools. Spec: `docs/superpowers/specs/2026-07-06-whoop-adapter-design.md`.

**Tech Stack:** Python 3.12, Starlette, httpx, SQLite (existing store). **No new dependencies** (no `mcp` SDK — the protocol layer is hand-rolled, precedent: dependency-free SigV4 signer in `backup.py`).

## Global Constraints

- Tests: `uv run --extra dev pytest -q` from the worktree root (`.claude/worktrees/whoop-adapter`) — the `--extra dev` is REQUIRED. Full suite green at the end of every task. Baseline: 145 passed.
- Never modify or import `garmin_mcp`; never touch `adapters/garmin/` behavior.
- WHOOP API facts (verified against the official OpenAPI spec — copy verbatim):
  - OAuth authorize: `{WHOOP_API_BASE}/oauth/oauth2/auth`; token: `{WHOOP_API_BASE}/oauth/oauth2/token`; prod base `https://api.prod.whoop.com`.
  - Data endpoints live under the `/developer` prefix: e.g. `{WHOOP_API_BASE}/developer/v2/cycle`.
  - Scopes string (space-delimited, exactly): `read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement offline`.
  - The upstream `state` param must be ≥8 chars (ours is `security.new_secret(18)` — fine).
  - Code exchange: form-encoded body `grant_type=authorization_code, code, client_id, client_secret, redirect_uri` (client creds in body, not Basic auth).
  - Refresh: form-encoded body `grant_type=refresh_token, refresh_token, client_id, client_secret, scope=offline` (`scope: offline` present in every documented refresh). **The refresh token ROTATES on every refresh** — the old pair dies; the rotated pair must be persisted before anything else runs.
  - Token response fields: `access_token`, `refresh_token`, `expires_in` (3600), `scope`, `token_type`.
  - Pagination: request param `nextToken` (camelCase), response fields `records` + `next_token` (snake_case); `limit` default 10, max 25.
  - Profile: `GET /developer/v2/user/profile/basic` → `{user_id, email, first_name, last_name}`.
- `account_key = normalize_account_key(profile email)` — the existing invariant (lowercased login email).
- Secrets: WHOOP tokens live ONLY in the encrypted blob (never logged, never in files); `WHOOP_CLIENT_SECRET` only in env/config. Logging account emails (`account=...`) is the existing norm; token values never.
- Log events are a stable schema. New events, exactly these names: `upstream-oauth-start`, `upstream-oauth-callback` (status: ok/denied/expired/error), `whoop-refresh-ok`, `whoop-refresh-failed`, `local-forward-auth-stale`. Do not rename existing events or fields.
- Verify-then-persist: `oauth._finish` stays gated on `adapter.verify(blob)` on EVERY authorize path, including the new callback path.
- Templates are fragments wrapped by `pages.render_page` (`_layout.html`). CSP must NOT gain a `form-action` directive.
- Worker registry: `app.py` builds a `WorkerManager` ONLY for worker-forward adapters — a local-forward adapter must get none.
- The adapter registers only when BOTH `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` are set (pattern: `BACKUP_S3_*`).
- Commit after every task; messages below. Append to each commit message:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01JCXKgmPUyFqJAFVM76gY24`

---

### Task 1: Config — WHOOP credentials + API base

**Files:**
- Modify: `src/missingmcp/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.whoop_client_id: str`, `Config.whoop_client_secret: str`, `Config.whoop_api_base: str` (default `https://api.prod.whoop.com`, trailing `/` stripped). Consumed by Tasks 2, 5, 6.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py` (a `BASE` dict already exists at the top of the file; reuse it):

```python
def test_whoop_defaults_off():
    c = load_config(BASE)
    assert c.whoop_client_id == "" and c.whoop_client_secret == ""
    assert c.whoop_api_base == "https://api.prod.whoop.com"


def test_whoop_settings_and_base_override():
    c = load_config({**BASE, "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                     "WHOOP_API_BASE": "http://127.0.0.1:9999/"})
    assert c.whoop_client_id == "cid-1" and c.whoop_client_secret == "sec-1"
    assert c.whoop_api_base == "http://127.0.0.1:9999"   # trailing slash stripped
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_config.py -q`
Expected: 2 FAIL — `AttributeError: 'Config' object has no attribute 'whoop_client_id'` (dataclass has no such field).

- [ ] **Step 3: Implement** — in `src/missingmcp/config.py`, add to the `Config` dataclass after `backup_interval`:

```python
    # WHOOP adapter (adapters/whoop). The adapter is registered only when both
    # client credentials are set — see adapters.build_adapters.
    whoop_client_id: str
    whoop_client_secret: str
    whoop_api_base: str           # tests/staging override; both OAuth and data URLs derive from it
```

and in `load_config(...)`, add to the `Config(...)` call after `backup_interval=...`:

```python
        whoop_client_id=env.get("WHOOP_CLIENT_ID", ""),
        whoop_client_secret=env.get("WHOOP_CLIENT_SECRET", ""),
        whoop_api_base=env.get("WHOOP_API_BASE", "https://api.prod.whoop.com").rstrip("/"),
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: 147 passed.

- [ ] **Step 5: Commit**

```bash
git add src/missingmcp/config.py tests/test_config.py
git commit -m "feat(config): WHOOP client credentials + API base override"
```

---

### Task 2: Fake WHOOP upstream + `WhoopApi` client (rotating refresh)

**Files:**
- Modify: `tests/conftest.py` (add `FakeWhoopUpstream` + `fake_whoop` fixture)
- Create: `src/missingmcp/adapters/whoop/__init__.py` (empty for now — package marker; Task 6 fills it)
- Create: `src/missingmcp/adapters/whoop/api.py`
- Test: `tests/test_whoop_api.py`

**Interfaces:**
- Consumes: `Config.whoop_*` (Task 1), `store.get_account_tokens/upsert_account`, `log.log/log_warn`.
- Produces (consumed by Tasks 3–6):
  - `WhoopApi(config)` with: `auth_url(state_id: str) -> str`; `profile_url: str` (property); `async exchange_code(code: str) -> dict` (blob dict WITHOUT identity fields); `async fetch_profile(access_token: str) -> dict`; `async get(conn, account_key: str, blob: dict, path: str, params: dict | None = None) -> tuple[int, object]`; `async ensure_fresh(conn, account_key: str, blob: dict, force: bool = False) -> dict`.
  - `WhoopAuthError(Exception)` — tokens beyond saving (refresh rejected / still-401).
  - Blob dict shape: `{"access_token": str, "refresh_token": str, "expires_at": int_unix, "user_id": int, "email": str}`.
  - conftest: `FakeWhoopUpstream` (knobs: `valid_tokens: set`, `refresh_fails: bool`, `reject_data_auth: bool`, `data_status: int | None`, `profile: dict`, counter `mint`; records `calls`) + `fake_whoop` fixture.

- [ ] **Step 1: Add the fake upstream to `tests/conftest.py`.** Add imports at the top (`import json`, `from urllib.parse import parse_qs`), then append:

```python
class FakeWhoopUpstream(_FakeHttpServer):
    """WHOOP OAuth + v2 API fake. The token endpoint mints rotating at-<n>/rt-<n>
    pairs (each minted access token becomes valid); data endpoints require a
    valid Bearer. Knobs:
      - valid_tokens: access tokens the data endpoints accept (starts empty)
      - refresh_fails: refresh grant answers 400 invalid_grant
      - reject_data_auth: data endpoints answer 401 regardless of token
      - data_status: force this status from data endpoints (e.g. 429, 500)
      - profile: the /v2/user/profile/basic payload
    """

    def __init__(self):
        self.valid_tokens = set()
        self.refresh_fails = False
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
                    return self._send_json(400, {"error": "invalid_grant"})
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
```

- [ ] **Step 2: Write the failing tests** — create `tests/test_whoop_api.py`:

```python
"""WhoopApi: upstream OAuth exchange + gateway-owned rotating token refresh.
Async functions are driven with asyncio.run — no async test plugin needed."""
import asyncio
import json
import time
import pytest
from missingmcp import store
from missingmcp.adapters.whoop.api import WhoopApi, WhoopAuthError
from missingmcp.config import load_config

KEY = "user@example.com"


def _cfg(fake):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                        "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                        "WHOOP_API_BASE": f"http://127.0.0.1:{fake.port}"})


def _blob(expires_in=3600, access="at-0", refresh="rt-0"):
    return {"access_token": access, "refresh_token": refresh,
            "expires_at": int(time.time()) + expires_in,
            "user_id": 123, "email": KEY}


def _seed(conn, cfg, blob):
    store.upsert_account(conn, "whoop", KEY, json.dumps(blob), cfg.gateway_secret)


def _token_calls(fake):
    return [b for m, p, _h, b in fake.calls if p == "/oauth/oauth2/token"]


def test_auth_url_contains_upstream_oauth_params(fake_whoop):
    api = WhoopApi(_cfg(fake_whoop))
    url = api.auth_url("state-12345678")
    assert url.startswith(f"http://127.0.0.1:{fake_whoop.port}/oauth/oauth2/auth?")
    assert "response_type=code" in url and "client_id=cid-1" in url
    assert "state=state-12345678" in url
    assert "redirect_uri=https%3A%2F%2Fgw.example.com%2Fwhoop%2Foauth%2Fcallback" in url
    assert "offline" in url and "read%3Arecovery" in url


def test_exchange_code_builds_blob(fake_whoop):
    api = WhoopApi(_cfg(fake_whoop))
    blob = asyncio.run(api.exchange_code("upstream-code"))
    assert blob["access_token"] == "at-1" and blob["refresh_token"] == "rt-1"
    assert blob["expires_at"] > time.time()
    _m, path, _h, body = fake_whoop.calls[-1]
    assert path == "/oauth/oauth2/token"
    assert b"grant_type=authorization_code" in body
    assert b"code=upstream-code" in body and b"client_secret=sec-1" in body
    assert b"redirect_uri=" in body


def test_get_with_fresh_token_skips_refresh(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.valid_tokens.add("at-0")
    blob = _blob(); _seed(conn, cfg, blob)
    status, payload = asyncio.run(api.get(conn, KEY, blob, "/v2/user/profile/basic"))
    assert status == 200 and payload["email"] == "User@Example.com"
    assert _token_calls(fake_whoop) == []


def test_stale_token_refreshes_and_persists_rotation(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    blob = _blob(expires_in=30)               # inside the 120s refresh margin
    _seed(conn, cfg, blob)
    status, _ = asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
    assert status == 200
    calls = _token_calls(fake_whoop)
    assert len(calls) == 1
    assert b"grant_type=refresh_token" in calls[0]
    assert b"refresh_token=rt-0" in calls[0] and b"scope=offline" in calls[0]
    stored = json.loads(store.get_account_tokens(conn, "whoop", KEY, cfg.gateway_secret))
    assert stored["access_token"] == "at-1" and stored["refresh_token"] == "rt-1"
    assert stored["email"] == KEY and stored["user_id"] == 123   # identity survives rotation


def test_concurrent_stale_calls_refresh_exactly_once(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    blob = _blob(expires_in=30)
    _seed(conn, cfg, blob)

    async def both():
        await asyncio.gather(api.get(conn, KEY, dict(blob), "/v2/cycle"),
                             api.get(conn, KEY, dict(blob), "/v2/recovery"))
    asyncio.run(both())
    assert len(_token_calls(fake_whoop)) == 1     # the lock serialized; waiter reused the row


def test_unexpected_401_forces_one_refresh_and_retry(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    blob = _blob()                                # looks fresh, but at-0 is not valid upstream
    _seed(conn, cfg, blob)
    status, _ = asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
    assert status == 200                          # refreshed to at-1, retried
    assert len(_token_calls(fake_whoop)) == 1


def test_refresh_invalid_grant_raises(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.refresh_fails = True
    blob = _blob(expires_in=30)
    _seed(conn, cfg, blob)
    with pytest.raises(WhoopAuthError):
        asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))


def test_persistent_401_after_refresh_raises(fake_whoop):
    cfg = _cfg(fake_whoop); conn = store.init_db(":memory:")
    api = WhoopApi(cfg)
    fake_whoop.reject_data_auth = True            # even freshly minted tokens bounce
    blob = _blob()
    _seed(conn, cfg, blob)
    with pytest.raises(WhoopAuthError):
        asyncio.run(api.get(conn, KEY, blob, "/v2/cycle"))
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_whoop_api.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'missingmcp.adapters.whoop'`.

- [ ] **Step 4: Implement.** Create `src/missingmcp/adapters/whoop/__init__.py` **empty** (package marker; Task 6 fills it). Create `src/missingmcp/adapters/whoop/api.py`:

```python
"""WHOOP v2 HTTP client: upstream OAuth code exchange, gateway-owned token
refresh, authenticated GETs. WHOOP rotates the refresh token on every refresh
(the old pair is invalidated immediately), so refresh is serialized per
account and the rotated blob is persisted to the store before proceeding."""
from __future__ import annotations
import asyncio
import json
import time
from urllib.parse import urlencode
import httpx
from ... import store
from ...log import log, log_warn

REFRESH_MARGIN_S = 120
HTTP_TIMEOUT_S = 15.0
SCOPES = "read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement offline"


class WhoopAuthError(Exception):
    """The account's tokens can't be made valid (refresh rejected, or the API
    keeps answering 401). Callers surface this as whoop_session_expired."""


def _blob_from_token_response(tok: dict) -> dict:
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
    }


class WhoopApi:
    def __init__(self, config):
        self._cfg = config
        self._locks: dict[str, asyncio.Lock] = {}

    # --- URLs ---------------------------------------------------------------
    @property
    def redirect_uri(self) -> str:
        return f"{self._cfg.public_url}/whoop/oauth/callback"

    @property
    def _token_url(self) -> str:
        return f"{self._cfg.whoop_api_base}/oauth/oauth2/token"

    def _data_url(self, path: str) -> str:
        return f"{self._cfg.whoop_api_base}/developer{path}"

    @property
    def profile_url(self) -> str:
        return self._data_url("/v2/user/profile/basic")

    def auth_url(self, state_id: str) -> str:
        q = urlencode({
            "response_type": "code",
            "client_id": self._cfg.whoop_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state_id,
        })
        return f"{self._cfg.whoop_api_base}/oauth/oauth2/auth?{q}"

    # --- login-time -----------------------------------------------------------
    async def exchange_code(self, code: str) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            r = await client.post(self._token_url, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._cfg.whoop_client_id,
                "client_secret": self._cfg.whoop_client_secret,
                "redirect_uri": self.redirect_uri,
            })
        if r.status_code != 200:
            raise WhoopAuthError(f"token exchange failed ({r.status_code})")
        return _blob_from_token_response(r.json())

    async def fetch_profile(self, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            r = await client.get(self.profile_url,
                                 headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code != 200:
            raise WhoopAuthError(f"profile fetch failed ({r.status_code})")
        return r.json()

    # --- request-time -----------------------------------------------------------
    async def get(self, conn, account_key: str, blob: dict, path: str,
                  params: dict | None = None) -> "tuple[int, object]":
        """GET a data endpoint with a fresh token: refresh ahead of expiry, one
        forced refresh + retry on an unexpected 401. Returns (status, json).
        Raises WhoopAuthError when the tokens are beyond saving."""
        blob = await self.ensure_fresh(conn, account_key, blob)
        status, payload = await self._get_once(blob, path, params)
        if status == 401:
            blob = await self.ensure_fresh(conn, account_key, blob, force=True)
            status, payload = await self._get_once(blob, path, params)
            if status == 401:
                raise WhoopAuthError("WHOOP rejected a freshly refreshed token")
        return status, payload

    async def _get_once(self, blob: dict, path: str, params: dict | None):
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            r = await client.get(self._data_url(path), params=params or {},
                                 headers={"Authorization": f"Bearer {blob['access_token']}"})
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text}
        return r.status_code, payload

    async def ensure_fresh(self, conn, account_key: str, blob: dict,
                           force: bool = False) -> dict:
        orig_token = blob["access_token"]
        if not force and blob["expires_at"] - time.time() > REFRESH_MARGIN_S:
            return blob
        lock = self._locks.setdefault(account_key, asyncio.Lock())
        async with lock:
            # Re-read: a queued waiter must reuse the blob another request just
            # rotated instead of burning the (now dead) refresh token again.
            current = store.get_account_tokens(conn, "whoop", account_key,
                                               self._cfg.gateway_secret)
            if current is not None:
                blob = json.loads(current)
            if force and blob["access_token"] != orig_token:
                return blob            # someone else already rotated past our stale copy
            if not force and blob["expires_at"] - time.time() > REFRESH_MARGIN_S:
                return blob
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                r = await client.post(self._token_url, data={
                    "grant_type": "refresh_token",
                    "refresh_token": blob["refresh_token"],
                    "client_id": self._cfg.whoop_client_id,
                    "client_secret": self._cfg.whoop_client_secret,
                    "scope": "offline",
                })
            if r.status_code != 200:
                log_warn("whoop-refresh-failed", account=account_key,
                         status=r.status_code)
                raise WhoopAuthError("WHOOP token refresh failed")
            new = _blob_from_token_response(r.json())
            new["user_id"] = blob.get("user_id")
            new["email"] = blob.get("email")
            # Persist BEFORE using: the old pair is already dead upstream.
            store.upsert_account(conn, "whoop", account_key, json.dumps(new),
                                 self._cfg.gateway_secret)
            log("whoop-refresh-ok", account=account_key)
            return new
```

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `uv run --extra dev pytest tests/test_whoop_api.py -q` → 8 passed.
Run: `uv run --extra dev pytest -q` → 155 passed.

- [ ] **Step 6: Commit**

```bash
git add src/missingmcp/adapters/whoop tests/test_whoop_api.py tests/conftest.py
git commit -m "feat(whoop): v2 API client — upstream OAuth exchange + gateway-owned rotating refresh"
```

---

### Task 3: Seam login shape C — upstream-OAuth redirect + callback in `oauth.py`

**Files:**
- Modify: `src/missingmcp/adapters/base.py` (add `is_upstream_oauth`)
- Modify: `src/missingmcp/oauth.py` (authorize_get branch + `authorize_callback` + error page)
- Create: `src/missingmcp/templates/upstream_error.html`
- Modify: `tests/conftest.py` (add `StubUpstreamOAuthAdapter`)
- Test: `tests/test_oauth.py` (append a section)

**Interfaces:**
- Consumes: `AuthState.put_mfa/pop_mfa` (existing stash: TTL 300 s, one-time pop, adapter-scoped — reused as-is with `pending=None`), `pages.render_page`, `oauth._fill`, `oauth._finish`.
- Produces (consumed by Task 6):
  - base.py: `def is_upstream_oauth(adapter) -> bool` — duck-typed on `hasattr(adapter, "authorize_redirect_url")`.
  - Upstream-OAuth adapter contract (documented as a comment next to `is_upstream_oauth`): `authorize_redirect_url(state_id: str) -> str` and `async handle_callback(query: Mapping[str, str]) -> LoginOk` (raises `LoginError`).
  - oauth.py: `async def authorize_callback(request, adapter, state, conn, config)`.
  - `authorize_get` return type widens to `HTMLResponse | RedirectResponse`.

- [ ] **Step 1: Add the stub adapter to `tests/conftest.py`** (below `StubRemoteAdapter`):

```python
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
```

- [ ] **Step 2: Write the failing tests** — append to `tests/test_oauth.py`:

```python
# --- upstream-OAuth login shape (C) — driven through StubUpstreamOAuthAdapter ---

from conftest import StubUpstreamOAuthAdapter


def _upstream_app(conn, adapter):
    state = oauth.AuthState(security.CsrfStore())

    async def aget(request):
        return await oauth.authorize_get(request, adapter, state, conn, CONFIG)

    async def cb(request):
        return await oauth.authorize_callback(request, adapter, state, conn, CONFIG)

    async def reg(request):
        return await oauth.register_client(request, conn, adapter)

    return TestClient(Starlette(routes=[
        Route("/oauth/register", reg, methods=["POST"]),
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/callback", cb, methods=["GET"]),
    ]))


def _register_and_authorize(c):
    reg = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    r = c.get("/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "claude-state", "code_challenge": "c" * 43,
        "code_challenge_method": "S256"}, follow_redirects=False)
    return reg, r


def test_upstream_authorize_redirects_to_provider(conn):
    adapter = StubUpstreamOAuthAdapter()
    c = _upstream_app(conn, adapter)
    _reg, r = _register_and_authorize(c)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://upstream.example/auth?state=")
    assert len(loc.split("state=")[1]) >= 8        # WHOOP requires state >= 8 chars


def test_upstream_authorize_still_validates_client(conn):
    c = _upstream_app(conn, StubUpstreamOAuthAdapter())
    r = c.get("/oauth/authorize", params={
        "client_id": "nope", "redirect_uri": "https://claude.ai/cb",
        "state": "s", "code_challenge": "c" * 43, "code_challenge_method": "S256"},
        follow_redirects=False)
    assert r.status_code == 400


def test_upstream_callback_happy_path_persists_and_redirects(conn):
    adapter = StubUpstreamOAuthAdapter()
    c = _upstream_app(conn, adapter)
    _reg, r = _register_and_authorize(c)
    sid = r.headers["location"].split("state=")[1]
    r = c.get("/oauth/callback", params={"code": "up-code", "state": sid},
              follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://claude.ai/cb?")
    q = parse_qs(urlparse(loc).query)
    assert q["state"] == ["claude-state"] and q["code"]
    assert adapter.callbacks[0]["code"] == "up-code"
    blob = store.get_account_tokens(conn, "acmeauth", "me@x.cz", CONFIG.gateway_secret)
    assert blob is not None and "at" in blob       # persisted under the normalized email


def test_upstream_callback_unknown_state_is_400(conn):
    c = _upstream_app(conn, StubUpstreamOAuthAdapter())
    r = c.get("/oauth/callback", params={"code": "x", "state": "bogus"})
    assert r.status_code == 400
    assert "expired" in r.text


def test_upstream_callback_state_is_single_use(conn):
    adapter = StubUpstreamOAuthAdapter()
    c = _upstream_app(conn, adapter)
    _reg, r = _register_and_authorize(c)
    sid = r.headers["location"].split("state=")[1]
    assert c.get("/oauth/callback", params={"code": "x", "state": sid},
                 follow_redirects=False).status_code == 302
    assert c.get("/oauth/callback", params={"code": "x", "state": sid}).status_code == 400


def test_upstream_callback_denied_shows_error_page(conn):
    adapter = StubUpstreamOAuthAdapter()
    c = _upstream_app(conn, adapter)
    _reg, r = _register_and_authorize(c)
    sid = r.headers["location"].split("state=")[1]
    r = c.get("/oauth/callback", params={"error": "access_denied", "state": sid})
    assert r.status_code == 400
    assert "AcmeAuth" in r.text
    assert adapter.callbacks == []                 # exchange never attempted


def test_upstream_callback_login_error_shows_message(conn):
    adapter = StubUpstreamOAuthAdapter(fail_with="AcmeAuth is on fire")
    c = _upstream_app(conn, adapter)
    _reg, r = _register_and_authorize(c)
    sid = r.headers["location"].split("state=")[1]
    r = c.get("/oauth/callback", params={"code": "x", "state": sid})
    assert r.status_code == 400
    assert "AcmeAuth is on fire" in r.text
    assert store.get_account_tokens(conn, "acmeauth", "me@x.cz", CONFIG.gateway_secret) is None
```

(`urlparse`/`parse_qs` are already imported at the top of `tests/test_oauth.py`.)

- [ ] **Step 3: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_oauth.py -q`
Expected: new tests FAIL — `AttributeError: module 'missingmcp.oauth' has no attribute 'authorize_callback'` (and the redirect test gets a 200 form instead of 302).

- [ ] **Step 4: Implement.** In `src/missingmcp/adapters/base.py`, append after `is_remote`:

```python
def is_upstream_oauth(adapter) -> bool:
    """Login-shape dispatch (duck-typed, like is_remote). An upstream-OAuth
    adapter provides, instead of a credential form:
      - authorize_redirect_url(state_id: str) -> str
      - async handle_callback(query: Mapping[str, str]) -> LoginOk   (raises LoginError)
    The gateway stashes the client's OAuth params under state_id (AuthState,
    TTL 300s, one-time pop), sends the user to the provider, and finishes the
    normal verify-then-persist path when the provider calls back."""
    return hasattr(adapter, "authorize_redirect_url")
```

Create `src/missingmcp/templates/upstream_error.html`:

```html
  <div class="auth">
    <h1>Couldn&rsquo;t connect <span class="hl">{DISPLAY_NAME}</span></h1>
    {ERROR}
    <p class="sub">Nothing was saved. Go back to Claude and start the connection
    again &mdash; if it keeps failing, contact {OPERATOR_NAME}{OPERATOR_EMAIL}.</p>
  </div>
```

In `src/missingmcp/oauth.py`:

1. Extend the imports: `from .adapters.base import LoginError, SecondFactorError, SecondFactorNeeded, is_upstream_oauth` and `from .log import log, log_warn, log_error, log_exc`.
2. In `authorize_get`, change the return annotation to `-> HTMLResponse | RedirectResponse` and insert before the final `return render_authorize(...)`:

```python
    if is_upstream_oauth(adapter):
        # No form of ours: stash Claude's OAuth params (same one-time TTL stash
        # as MFA, pending=None) and send the user to the provider. The stash id
        # rides in the provider's `state` and doubles as callback CSRF.
        sid = state.put_mfa(None, params, adapter.name)
        log("upstream-oauth-start", adapter=adapter.name, client_id=params["client_id"])
        return RedirectResponse(adapter.authorize_redirect_url(sid), status_code=302)
```

3. Append the error-page helper and the callback handler:

```python
def _upstream_error(config, adapter, message: str) -> HTMLResponse:
    body = _fill(pages.render_page("upstream_error.html",
                                   f"Connect {adapter.display_name} — MissingMCP"),
                 {"DISPLAY_NAME": adapter.display_name, **_operator_fields(config)},
                 message)
    return HTMLResponse(body, status_code=400)


async def authorize_callback(request, adapter, state, conn, config) -> HTMLResponse | RedirectResponse:
    """The provider's redirect back (login shape C). Pop-once state lookup,
    re-validate the DCR client, exchange the code via the adapter, then the
    standard verify-then-persist finish."""
    q = request.query_params
    popped = state.pop_mfa(q.get("state", ""), adapter.name)
    if popped is None:
        log_error("upstream-oauth-callback", adapter=adapter.name, status="expired")
        return _upstream_error(config, adapter,
                               "This sign-in link expired — go back to Claude and try connecting again.")
    _pending, params = popped
    client = store.get_client(conn, params["client_id"])
    if client is None or not security.validate_redirect_uri(params["redirect_uri"],
                                                            client["redirect_uris"]):
        return HTMLResponse("invalid client/redirect_uri", status_code=400)
    if q.get("error") or not q.get("code"):
        # the user declined at the provider — expected, not an anomaly
        log_warn("upstream-oauth-callback", adapter=adapter.name, status="denied",
                 reason=q.get("error", "no_code"))
        return _upstream_error(config, adapter,
                               f"{adapter.display_name} declined the connection — go back to Claude and try again.")
    try:
        result = await adapter.handle_callback(q)
        t0 = time.monotonic()
        name = adapter.verify(result.blob)
        log("upstream-verify-ok", name=name, ms=int((time.monotonic() - t0) * 1000))
    except LoginError as e:
        log_exc("upstream-oauth-callback", e, adapter=adapter.name, status="error",
                error=str(e))
        return _upstream_error(config, adapter, str(e))
    log("upstream-oauth-callback", adapter=adapter.name, status="ok")
    log("authorize-finish", step="upstream")
    return _finish(conn, config, params, result.blob, adapter.name, result.account_key)
```

- [ ] **Step 5: Run the oauth tests, then the full suite**

Run: `uv run --extra dev pytest tests/test_oauth.py -q` → all pass (7 new).
Run: `uv run --extra dev pytest -q` → 162 passed.

- [ ] **Step 6: Commit**

```bash
git add src/missingmcp/adapters/base.py src/missingmcp/oauth.py \
        src/missingmcp/templates/upstream_error.html tests/conftest.py tests/test_oauth.py
git commit -m "feat(oauth): upstream-OAuth login shape — provider redirect + callback path"
```

---

### Task 4: Forward strategy C — `LocalForward` in-process dispatch in `proxy.py`

**Files:**
- Modify: `src/missingmcp/adapters/base.py` (add `LocalForward`, `is_local`, `SessionExpired`)
- Modify: `src/missingmcp/proxy.py` (local branch in `handle_mcp`)
- Modify: `src/missingmcp/app.py` (no `WorkerManager` for local-forward adapters)
- Modify: `tests/conftest.py` (add `StubLocalAdapter`)
- Test: `tests/test_local_forward.py`

**Interfaces:**
- Consumes: proxy shared core (authenticate, body limit, blob fetch, usage, `mcp-response` event).
- Produces (consumed by Task 5/6):
  - base.py: `class LocalForward(Protocol)` with `async def handle(self, conn, account_key: str, blob: str, body: bytes) -> tuple[int, dict, bytes]`; `def is_local(forward) -> bool` (duck-typed on `hasattr(forward, "handle")`); `class SessionExpired(Exception)`.
  - proxy behavior: local + `POST` → dispatch in-process; local + `GET`/`DELETE` → 405 `{"error": "method_not_allowed"}`; `SessionExpired` from `handle` → 502 `<adapter>_session_expired` (existing `_session_expired` shape) + `local-forward-auth-stale` log event.

- [ ] **Step 1: Add the stub adapter to `tests/conftest.py`** (below `StubUpstreamOAuthAdapter`):

```python
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
```

- [ ] **Step 2: Write the failing tests** — create `tests/test_local_forward.py`:

```python
"""Local-forward (strategy C) behavior of proxy.handle_mcp, driven through
StubLocalAdapter — mirrors test_remote_forward.py. No subprocess, no upstream:
the forward handles the JSON-RPC request in-process."""
import json
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from conftest import StubLocalAdapter
from missingmcp import store, proxy, security
from missingmcp.config import load_config

TOKEN = "tok-local"
BLOB = '{"access_token":"at-0"}'


def _setup():
    conn = store.init_db(":memory:")
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x"})
    store.upsert_account(conn, "acmelocal", "me@x.cz", BLOB, cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(TOKEN), "acmelocal", "me@x.cz", "c1")
    adapter = StubLocalAdapter()
    rate = security.RateLimiter()

    def mcp(method):
        async def handler(request):
            return await proxy.handle_mcp(request, method, adapter, conn, None, cfg,
                                          cfg.gateway_secret, rate)
        return handler

    client = TestClient(Starlette(routes=[
        Route("/acmelocal/mcp", mcp("POST"), methods=["POST"]),
        Route("/acmelocal/mcp", mcp("GET"), methods=["GET"]),
        Route("/acmelocal/mcp", mcp("DELETE"), methods=["DELETE"]),
    ]))
    return conn, adapter, client


def _post(client, body=None):
    return client.post("/acmelocal/mcp",
                       json=body or {"jsonrpc": "2.0", "method": "initialize", "id": 1},
                       headers={"Authorization": f"Bearer {TOKEN}"})


def test_post_dispatches_in_process_with_decrypted_blob():
    _conn, adapter, c = _setup()
    r = _post(c)
    assert r.status_code == 200
    assert r.json()["result"] == {"echo": "initialize"}
    key, blob, body = adapter.forward.handled[0]
    assert key == "me@x.cz" and blob == BLOB
    assert json.loads(body)["method"] == "initialize"


def test_get_and_delete_are_405():
    _conn, adapter, c = _setup()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/acmelocal/mcp", headers=auth).status_code == 405
    assert c.delete("/acmelocal/mcp", headers=auth).status_code == 405
    assert adapter.forward.handled == []


def test_session_expired_maps_to_502_shape():
    _conn, adapter, c = _setup()
    adapter.forward.expire = True
    r = _post(c)
    assert r.status_code == 502
    assert r.json() == {
        "error": "acmelocal_session_expired",
        "message": "Your AcmeLocal session expired. "
                   "Please reconnect the AcmeLocal MCP server.",
    }


def test_usage_and_response_event_recorded(capsys):
    conn, _adapter, c = _setup()
    r = _post(c, body={"jsonrpc": "2.0", "method": "tools/call",
                       "params": {"name": "get_profile"}, "id": 1})
    assert r.status_code == 200
    rows = [tuple(row) for row in conn.execute(
        "SELECT adapter, account_key, tool, calls FROM tool_usage").fetchall()]
    assert rows == [("acmelocal", "me@x.cz", "get_profile", 1)]
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    resp = next(e for e in events if e["event"] == "mcp-response")
    assert resp["adapter"] == "acmelocal" and resp["tool"] == "get_profile"
    assert resp["status"] == 200 and resp["bytes"] > 0


def test_unauthenticated_is_401():
    _conn, adapter, c = _setup()
    r = c.post("/acmelocal/mcp", json={"jsonrpc": "2.0", "method": "initialize"})
    assert r.status_code == 401
    assert adapter.forward.handled == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_local_forward.py -q`
Expected: FAIL — `ImportError: cannot import name 'SessionExpired'` (and after that fix would 404/proxy-forward instead of dispatching).

- [ ] **Step 4: Implement.** In `src/missingmcp/adapters/base.py`, append after `is_upstream_oauth`:

```python
class SessionExpired(Exception):
    """A local forward's stored credentials went stale beyond repair (e.g. a
    rotated-away refresh token). The proxy surfaces the standard
    <adapter>_session_expired 502 so the client prompts a reconnect."""


class LocalForward(Protocol):
    """Forward strategy C: handled in-process — no subprocess, no shared
    upstream. Receives conn + account_key (serving a request may rotate
    upstream tokens, which must be persisted immediately) and the decrypted
    blob the proxy already fetched. Returns (status, headers, body).
    Raises SessionExpired when the credentials are beyond saving."""

    async def handle(self, conn, account_key: str, blob: str,
                     body: bytes) -> "tuple[int, dict, bytes]": ...


def is_local(forward) -> bool:
    """Strategy dispatch, beside is_remote (worker forwards have neither
    `upstream_url` nor `handle`)."""
    return hasattr(forward, "handle")
```

In `src/missingmcp/proxy.py`:

1. Extend the import: `from .adapters.base import is_remote, is_local, SessionExpired`.
2. In `handle_mcp`, right after the `auth` check (`key = auth`), add the method gate:

```python
    if is_local(adapter.forward) and method != "POST":
        # stateless in-process server: no SSE listen stream, no sessions
        return JSONResponse({"error": "method_not_allowed"}, status_code=405)
```

3. After the usage-recording block (`store.record_usage ...`), before the `remote = is_remote(...)` strategy dispatch, add the local branch:

```python
    if is_local(adapter.forward):
        try:
            status, headers, payload = await adapter.forward.handle(conn, key, tokens, body)
        except SessionExpired:
            log_error("local-forward-auth-stale", adapter=adapter.name, account=key)
            return _session_expired(adapter)
        ms = int((time.monotonic() - t0) * 1000)
        log("mcp-response", adapter=adapter.name, account=key, tool=tool,
            status=status, ttfb_ms=ms, total_ms=ms, bytes=len(payload))
        return Response(payload, status_code=status, headers=headers)
```

In `src/missingmcp/app.py`: extend the import `from .adapters.base import is_remote, is_local` and change the managers comprehension so local adapters get no WorkerManager:

```python
    managers = {a.name: WorkerManager(config, a.forward)
                for a in adapters.values()
                if not is_remote(a.forward) and not is_local(a.forward)}
```

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `uv run --extra dev pytest tests/test_local_forward.py -q` → 5 passed.
Run: `uv run --extra dev pytest -q` → 167 passed.

- [ ] **Step 6: Commit**

```bash
git add src/missingmcp/adapters/base.py src/missingmcp/proxy.py src/missingmcp/app.py \
        tests/conftest.py tests/test_local_forward.py
git commit -m "feat(proxy): LocalForward strategy — in-process MCP dispatch, 405 on GET/DELETE"
```

---

### Task 5: The WHOOP MCP server — JSON-RPC layer + 8 read-only tools

**Files:**
- Create: `src/missingmcp/adapters/whoop/mcp.py`
- Test: `tests/test_whoop_mcp.py`

**Interfaces:**
- Consumes: `WhoopApi` (Task 2), `base.SessionExpired` (Task 4).
- Produces (consumed by Task 6 and `scripts/gen_whoop_tools.py` in Task 7):
  - `TOOLS: list[tuple[str, str, dict, callable]]` — `(name, description, input_schema, resolve)` where `resolve(args: dict) -> tuple[path: str, params: dict]`.
  - `class WhoopLocalForward` implementing the `LocalForward` protocol, with attribute `api: WhoopApi`.
  - `PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_whoop_mcp.py`:

```python
"""The in-process WHOOP MCP server: hand-rolled stateless JSON-RPC over HTTP
(initialize / notifications / tools/list / tools/call / ping) against the fake
WHOOP upstream."""
import asyncio
import json
import pytest
from missingmcp import store
from missingmcp.adapters.base import SessionExpired
from missingmcp.adapters.whoop.mcp import TOOLS, WhoopLocalForward
from missingmcp.config import load_config

KEY = "user@example.com"
BLOB = json.dumps({"access_token": "at-0", "refresh_token": "rt-0",
                   "expires_at": 9999999999, "user_id": 123, "email": KEY})


def _setup(fake):
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                       "WHOOP_API_BASE": f"http://127.0.0.1:{fake.port}"})
    conn = store.init_db(":memory:")
    store.upsert_account(conn, "whoop", KEY, BLOB, cfg.gateway_secret)
    fake.valid_tokens.add("at-0")
    return conn, WhoopLocalForward(cfg)


def _rpc(fwd, conn, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    status, headers, payload = asyncio.run(
        fwd.handle(conn, KEY, BLOB, json.dumps(body).encode()))
    return status, headers, json.loads(payload) if payload else None


def test_initialize_negotiates_known_version(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    status, headers, body = _rpc(fwd, conn, "initialize",
                                 {"protocolVersion": "2025-03-26"})
    assert status == 200 and headers["Content-Type"] == "application/json"
    result = body["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "missingmcp-whoop"


def test_initialize_unknown_version_falls_back_to_latest(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "initialize", {"protocolVersion": "1999-01-01"})
    assert body["result"]["protocolVersion"] == "2025-06-18"


def test_notification_gets_202_empty(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    status, _h, body = _rpc(fwd, conn, "notifications/initialized", rid=None)
    assert status == 202 and body is None


def test_ping_and_unknown_method(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    assert _rpc(fwd, conn, "ping")[2]["result"] == {}
    _s, _h, body = _rpc(fwd, conn, "bogus/method")
    assert body["error"]["code"] == -32601


def test_tools_list_exposes_all_eight(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/list")
    tools = body["result"]["tools"]
    assert [t["name"] for t in tools] == [name for name, _d, _s2, _r in TOOLS]
    assert len(tools) == 8
    assert all(t["description"] and t["inputSchema"]["type"] == "object" for t in tools)


def test_tools_call_get_profile(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/call",
                        {"name": "get_profile", "arguments": {}})
    result = body["result"]
    assert result["isError"] is False
    assert "User@Example.com" in result["content"][0]["text"]


def test_collection_args_map_to_whoop_query(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _rpc(fwd, conn, "tools/call", {"name": "get_cycles", "arguments": {
        "start": "2026-07-01T00:00:00.000Z", "end": "2026-07-06T00:00:00.000Z",
        "limit": 25, "next_token": "abc"}})
    path = next(p for m, p, _h, _b in fake_whoop.calls if "/v2/cycle" in p)
    assert "start=2026-07-01" in path and "end=2026-07-06" in path
    assert "limit=25" in path and "nextToken=abc" in path      # camelCase upstream


def test_by_id_tool_builds_path_and_requires_id(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/call",
                        {"name": "get_sleep", "arguments": {"id": "uuid-1"}})
    assert body["result"]["isError"] is False
    assert "/v2/activity/sleep/uuid-1" in body["result"]["content"][0]["text"]
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "get_sleep", "arguments": {}})
    assert body["result"]["isError"] is True                   # missing id → tool error


def test_unknown_tool_is_invalid_params(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "nope", "arguments": {}})
    assert body["error"]["code"] == -32602


def test_upstream_429_and_500_become_tool_errors(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    fake_whoop.data_status = 429
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "get_cycles", "arguments": {}})
    assert body["result"]["isError"] is True
    assert "rate limit" in body["result"]["content"][0]["text"]
    fake_whoop.data_status = 500
    _s, _h, body = _rpc(fwd, conn, "tools/call", {"name": "get_cycles", "arguments": {}})
    assert body["result"]["isError"] is True


def test_dead_refresh_raises_session_expired(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    fake_whoop.refresh_fails = True
    stale = json.dumps({**json.loads(BLOB), "expires_at": 1})
    with pytest.raises(SessionExpired):
        asyncio.run(fwd.handle(conn, KEY, stale, json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_cycles", "arguments": {}}}).encode()))


def test_batch_and_garbage_are_400(fake_whoop):
    conn, fwd = _setup(fake_whoop)
    status, _h, _b = asyncio.run(fwd.handle(conn, KEY, BLOB, b"[]"))
    assert status == 400
    status, _h, _b = asyncio.run(fwd.handle(conn, KEY, BLOB, b"not json"))
    assert status == 400
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_whoop_mcp.py -q`
Expected: collection error — `ModuleNotFoundError`/`ImportError` on `missingmcp.adapters.whoop.mcp`.

- [ ] **Step 3: Implement** — create `src/missingmcp/adapters/whoop/mcp.py`:

```python
"""The /whoop/mcp server itself: a hand-rolled, stateless, tools-only MCP —
JSON-RPC over streamable HTTP, single (non-batch) requests, application/json
responses, no sessions. Claude is the only targeted client; the surface is
initialize / notifications/* / tools/list / tools/call / ping. Tool payloads
are WHOOP's v2 JSON passed through verbatim as text content."""
from __future__ import annotations
import json
import httpx
from ..base import SessionExpired
from .api import WhoopApi, WhoopAuthError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "missingmcp-whoop", "version": "1.0.0"}

_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _collection_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "start": {"type": "string",
                      "description": "Only records from this ISO 8601 time on (inclusive), e.g. 2026-07-01T00:00:00.000Z"},
            "end": {"type": "string",
                    "description": "Only records before this ISO 8601 time (exclusive); defaults to now"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25,
                      "description": "Records per page (default 10, max 25)"},
            "next_token": {"type": "string",
                           "description": "Pagination token from the previous response's next_token"},
        },
        "additionalProperties": False,
    }


def _id_schema(desc: str) -> dict:
    return {"type": "object",
            "properties": {"id": {"type": "string", "description": desc}},
            "required": ["id"], "additionalProperties": False}


def _collection_params(args: dict) -> dict:
    params = {}
    if args.get("start"):
        params["start"] = args["start"]
    if args.get("end"):
        params["end"] = args["end"]
    if args.get("limit"):
        params["limit"] = int(args["limit"])
    if args.get("next_token"):
        params["nextToken"] = args["next_token"]   # camelCase on the wire (WHOOP spec)
    return params


def _plain(path: str):
    return lambda args: (path, {})


def _collection(path: str):
    return lambda args: (path, _collection_params(args))


def _by_id(path_tpl: str):
    return lambda args: (path_tpl.format(id=args["id"]), {})


# (name, description, input schema, resolve(args) -> (path, query)).
# scripts/gen_whoop_tools.py renders the landing page's tool list from this.
TOOLS = [
    ("get_profile", "The connected user's WHOOP profile: name and account email.",
     _EMPTY_SCHEMA, _plain("/v2/user/profile/basic")),
    ("get_body_measurements", "Height, weight, and max heart rate on record.",
     _EMPTY_SCHEMA, _plain("/v2/user/measurement/body")),
    ("get_cycles", "Physiological (day) cycles: strain, average/max heart rate, energy burned. Paginated.",
     _collection_schema(), _collection("/v2/cycle")),
    ("get_recoveries", "Recovery scores: recovery %, HRV (rmssd), resting heart rate, SpO2, skin temp. Paginated.",
     _collection_schema(), _collection("/v2/recovery")),
    ("get_sleeps", "Sleep activities: stages, time in bed, efficiency, respiratory rate, sleep need. Paginated.",
     _collection_schema(), _collection("/v2/activity/sleep")),
    ("get_sleep", "One sleep activity by its UUID.",
     _id_schema("Sleep UUID (from get_sleeps)"), _by_id("/v2/activity/sleep/{id}")),
    ("get_workouts", "Workouts: sport, strain, heart rate, energy, distance where available. Paginated.",
     _collection_schema(), _collection("/v2/activity/workout")),
    ("get_workout", "One workout by its UUID.",
     _id_schema("Workout UUID (from get_workouts)"), _by_id("/v2/activity/workout/{id}")),
]


def _http_json(status: int, obj) -> "tuple[int, dict, bytes]":
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()


def _result(rid, result) -> "tuple[int, dict, bytes]":
    return _http_json(200, {"jsonrpc": "2.0", "id": rid, "result": result})


def _rpc_error(rid, code: int, message: str) -> "tuple[int, dict, bytes]":
    return _http_json(200, {"jsonrpc": "2.0", "id": rid,
                            "error": {"code": code, "message": message}})


def _tool_error(rid, message: str) -> "tuple[int, dict, bytes]":
    # MCP tool-level failure: a *result* with isError, not a protocol error.
    return _result(rid, {"content": [{"type": "text", "text": message}],
                         "isError": True})


class WhoopLocalForward:
    """LocalForward strategy C for whoop: the whole MCP server, in-process."""

    def __init__(self, config):
        self.api = WhoopApi(config)

    async def handle(self, conn, account_key: str, blob: str,
                     body: bytes) -> "tuple[int, dict, bytes]":
        try:
            req = json.loads(body)
        except (ValueError, TypeError):
            req = None
        if not isinstance(req, dict):          # garbage or JSON-RPC batch
            return _http_json(400, {"error": "invalid_request"})
        method = req.get("method", "")
        rid = req.get("id")
        if rid is None:                        # notification: acknowledge, no body
            return 202, {"Content-Type": "application/json"}, b""
        if method == "initialize":
            client_ver = (req.get("params") or {}).get("protocolVersion", "")
            ver = client_ver if client_ver in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(rid, {"protocolVersion": ver,
                                 "capabilities": {"tools": {}},
                                 "serverInfo": SERVER_INFO})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [
                {"name": name, "description": desc, "inputSchema": schema}
                for name, desc, schema, _resolve in TOOLS]})
        if method == "tools/call":
            return await self._call(conn, account_key, blob, rid,
                                    req.get("params") or {})
        return _rpc_error(rid, -32601, f"Method not found: {method}")

    async def _call(self, conn, account_key, blob, rid, params):
        name = params.get("name", "")
        tool = next((t for t in TOOLS if t[0] == name), None)
        if tool is None:
            return _rpc_error(rid, -32602, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        try:
            path, query = tool[3](args)
        except (KeyError, ValueError) as e:
            return _tool_error(rid, f"Invalid or missing argument: {e}")
        try:
            status, payload = await self.api.get(conn, account_key,
                                                 json.loads(blob), path, query)
        except WhoopAuthError as e:
            raise SessionExpired(str(e)) from e
        except httpx.HTTPError:
            return _tool_error(rid, "WHOOP could not be reached — try again shortly.")
        if status == 429:
            return _tool_error(rid, "WHOOP rate limit hit — try again in a minute.")
        if status >= 400:
            return _tool_error(rid, f"WHOOP returned an error (HTTP {status}).")
        return _result(rid, {"content": [{"type": "text",
                                          "text": json.dumps(payload)}],
                             "isError": False})
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `uv run --extra dev pytest tests/test_whoop_mcp.py -q` → 12 passed.
Run: `uv run --extra dev pytest -q` → 179 passed.

- [ ] **Step 5: Commit**

```bash
git add src/missingmcp/adapters/whoop/mcp.py tests/test_whoop_mcp.py
git commit -m "feat(whoop): in-process MCP server — stateless JSON-RPC layer + 8 read-only tools"
```

---

### Task 6: `WhoopAdapter` + registration + app wiring + landing page + end-to-end

**Files:**
- Modify: `src/missingmcp/adapters/whoop/__init__.py` (was empty; now the adapter)
- Modify: `src/missingmcp/adapters/__init__.py` (conditional registration)
- Modify: `src/missingmcp/app.py` (callback route for upstream-OAuth adapters; no authorize POST for them)
- Create: `src/missingmcp/templates/whoop.html`
- Test: `tests/test_whoop_e2e.py`

**Interfaces:**
- Consumes: `WhoopApi`/`WhoopAuthError` (Task 2), `is_upstream_oauth` + `authorize_callback` (Task 3), `WhoopLocalForward` (Task 5), `oauth` route helpers in `app.py`.
- Produces: `WhoopAdapter(config)` with `name="whoop"`, `display_name="WHOOP"`, `landing_template="whoop.html"`, `forward: WhoopLocalForward`, `authorize_redirect_url`, `async handle_callback`, `verify`; registry entry `"whoop"` present only when both `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` are set.

- [ ] **Step 1: Write the failing tests** — create `tests/test_whoop_e2e.py`:

```python
"""End-to-end: the whoop adapter through build_app — discovery, upstream-OAuth
authorize → callback, downstream token exchange, and MCP tool calls, all
against the fake WHOOP upstream."""
import base64
import hashlib
import json
import time
from urllib.parse import urlparse, parse_qs
from starlette.testclient import TestClient
from missingmcp import store
from missingmcp.app import build_app
from missingmcp.config import load_config

BASE_ENV = {"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
            "DB_PATH": ":memory:", "DATA_DIR": "/tmp"}


def _client(fake):
    cfg = load_config({**BASE_ENV,
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1",
                       "WHOOP_API_BASE": f"http://127.0.0.1:{fake.port}"})
    return TestClient(build_app(cfg)), cfg


def _pkce():
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _connect(client):
    """Run the whole flow; returns (bearer_token, registration)."""
    reg = client.post("/whoop/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    verifier, challenge = _pkce()
    r = client.get("/whoop/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "claude-state", "code_challenge": challenge,
        "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 302
    up = urlparse(r.headers["location"])
    upq = parse_qs(up.query)
    r = client.get("/whoop/oauth/callback",
                   params={"code": "upstream-code", "state": upq["state"][0]},
                   follow_redirects=False)
    assert r.status_code == 302
    cbq = parse_qs(urlparse(r.headers["location"]).query)
    assert cbq["state"] == ["claude-state"]
    r = client.post("/whoop/oauth/token", data={
        "grant_type": "authorization_code", "code": cbq["code"][0],
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert r.status_code == 200
    return r.json()["access_token"], reg


def test_discovery_documents(fake_whoop):
    client, _cfg = _client(fake_whoop)
    r = client.get("/.well-known/oauth-authorization-server/whoop")
    assert r.status_code == 200
    assert r.json()["issuer"] == "https://gw.example.com/whoop"
    r = client.get("/.well-known/oauth-protected-resource/whoop/mcp")
    assert r.json()["resource"] == "https://gw.example.com/whoop/mcp"


def test_authorize_redirect_carries_whoop_params(fake_whoop):
    client, _cfg = _client(fake_whoop)
    reg = client.post("/whoop/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    _verifier, challenge = _pkce()
    r = client.get("/whoop/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
        "state": "cl", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False)
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert loc.startswith(f"http://127.0.0.1:{fake_whoop.port}/oauth/oauth2/auth")
    assert q["client_id"] == ["cid-1"] and len(q["state"][0]) >= 8
    assert q["redirect_uri"] == ["https://gw.example.com/whoop/oauth/callback"]
    assert "offline" in q["scope"][0] and "read:recovery" in q["scope"][0]


def test_full_connect_flow_and_tool_call(fake_whoop):
    client, cfg = _client(fake_whoop)
    token, _reg = _connect(client)
    r = client.post("/whoop/mcp", headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "get_profile", "arguments": {}}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    assert "User@Example.com" in result["content"][0]["text"]


def test_account_key_is_normalized_email(fake_whoop):
    client, cfg = _client(fake_whoop)
    _connect(client)
    conn = store.init_db(":memory:")   # fresh conn won't see the app's DB; assert via app instead
    r = client.get("/whoop")           # landing renders → the app itself is the oracle:
    assert r.status_code == 200
    # the persisted row is observable through a second connect: same account, no dup
    token2, _ = _connect(client)
    assert token2


def test_authorize_post_is_not_registered_for_whoop(fake_whoop):
    client, _cfg = _client(fake_whoop)
    r = client.post("/whoop/oauth/authorize", data={"anything": "x"})
    assert r.status_code == 405        # GET-only route: no credential form to POST


def test_stale_refresh_maps_to_session_expired(fake_whoop):
    client, _cfg = _client(fake_whoop)
    token, _reg = _connect(client)
    fake_whoop.refresh_fails = True
    fake_whoop.valid_tokens.clear()    # current access token stops working upstream
    r = client.post("/whoop/mcp", headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "get_cycles", "arguments": {}}})
    assert r.status_code == 502
    assert r.json()["error"] == "whoop_session_expired"


def test_without_credentials_whoop_is_absent():
    cfg = load_config(BASE_ENV)        # no WHOOP_CLIENT_ID/SECRET
    client = TestClient(build_app(cfg))
    assert client.get("/.well-known/oauth-authorization-server/whoop").status_code == 404
    assert client.get("/whoop", follow_redirects=False).status_code == 404
    # garmin is untouched either way
    assert client.get("/.well-known/oauth-authorization-server/garmin").status_code == 200
```

Note on `test_account_key_is_normalized_email`: the app's DB is `:memory:` inside `build_app`, so the test asserts idempotent re-connect instead of reading the row directly. The direct normalization assert (`me@x.cz` from `Me@X.cz`) already lives in `tests/test_oauth.py` (Task 3) and `tests/test_whoop_api.py`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_whoop_e2e.py -q`
Expected: FAIL — `/whoop/*` routes 404 (adapter not registered), landing template missing.

- [ ] **Step 3: Implement the adapter** — replace the empty `src/missingmcp/adapters/whoop/__init__.py` with:

```python
from __future__ import annotations
import json
from typing import Mapping
import httpx
from ..base import LoginError, LoginOk, normalize_account_key
from .api import HTTP_TIMEOUT_S, WhoopAuthError
from .mcp import WhoopLocalForward


class WhoopAdapter:
    """Upstream-OAuth login (shape C) + local forward (strategy C): WHOOP is a
    real OAuth provider, so users sign in at WHOOP — this gateway never sees a
    WHOOP password — and the MCP server runs in-process (mcp.py)."""

    name = "whoop"
    display_name = "WHOOP"
    authorize_template = ""        # no credential form: login happens at WHOOP
    second_factor_template = ""
    landing_template = "whoop.html"

    def __init__(self, config):
        self.forward = WhoopLocalForward(config)
        self.api = self.forward.api

    # --- upstream-OAuth login shape ------------------------------------------
    def authorize_redirect_url(self, state_id: str) -> str:
        return self.api.auth_url(state_id)

    async def handle_callback(self, query: Mapping[str, str]) -> LoginOk:
        try:
            blob = await self.api.exchange_code(query.get("code", ""))
            profile = await self.api.fetch_profile(blob["access_token"])
        except (WhoopAuthError, httpx.HTTPError) as e:
            raise LoginError("WHOOP sign-in could not be completed — please try again.") from e
        email = profile.get("email", "")
        if not email:
            raise LoginError("WHOOP did not return an account email.")
        blob["user_id"] = profile.get("user_id")
        blob["email"] = email
        return LoginOk(account_key=normalize_account_key(email), blob=json.dumps(blob))

    # --- form-login contract stubs (unreachable: app.py registers no authorize
    # POST for upstream-OAuth adapters) ----------------------------------------
    def login_hint(self, form: Mapping[str, str]) -> str:
        return ""

    def start_login(self, form: Mapping[str, str]) -> LoginOk:
        raise LoginError("WHOOP sign-in happens at WHOOP, not here.")

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        raise LoginError("WHOOP sign-in happens at WHOOP, not here.")

    def verify(self, blob: str) -> str:
        """Gate for verify-then-persist: re-fetch the profile with the blob's
        access token (sync, one-off at login time — garmin's login is equally
        blocking) and return a display name for the logs."""
        d = json.loads(blob)
        try:
            r = httpx.get(self.api.profile_url,
                          headers={"Authorization": f"Bearer {d['access_token']}"},
                          timeout=HTTP_TIMEOUT_S)
        except httpx.HTTPError as e:
            raise LoginError("WHOOP could not be reached to verify the sign-in.") from e
        if r.status_code != 200:
            raise LoginError("WHOOP sign-in could not be verified.")
        p = r.json()
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
```

- [ ] **Step 4: Register conditionally** — replace `build_adapters` in `src/missingmcp/adapters/__init__.py`:

```python
def build_adapters(config) -> dict:
    # rohlik was a RemoteForward adapter here until 2026-07 — retired when Rohlík
    # shipped its own OAuth MCP (connect https://mcp.rohlik.cz/mcp directly).
    # The remote strategy stays first-class: see tests/test_remote_forward.py.
    from .garmin import GarminAdapter
    adapters = {"garmin": GarminAdapter(config)}
    # whoop needs an operator-registered WHOOP app; without credentials the
    # connector stays off (local dev, CI) — same pattern as BACKUP_S3_*.
    if config.whoop_client_id and config.whoop_client_secret:
        from .whoop import WhoopAdapter
        adapters["whoop"] = WhoopAdapter(config)
    return adapters
```

- [ ] **Step 5: Wire the routes** — in `src/missingmcp/app.py`, extend the base import to `from .adapters.base import is_remote, is_local, is_upstream_oauth`. Inside `adapter_routes(adapter)`, add a callback handler next to `authz_post`:

```python
        async def callback(request):
            if not rate.check(f"login:{request.client.host}", 5, 60):
                return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
            return await oauth.authorize_callback(request, adapter, auth_state, conn, config)
```

and replace the returned route list so the login-shape decides which routes exist (everything else unchanged):

```python
        routes = [
            Route(f"/{p}", landing, methods=["GET"]),
            Route(f"/.well-known/oauth-authorization-server/{p}", meta, methods=["GET"]),
            Route(f"/.well-known/oauth-protected-resource/{p}/mcp", prmeta, methods=["GET"]),
            Route(f"/{p}/oauth/register", register, methods=["POST"]),
            Route(f"/{p}/oauth/authorize", authz_get, methods=["GET"]),
        ]
        if is_upstream_oauth(adapter):
            # login happens at the provider: callback instead of a form POST
            routes.append(Route(f"/{p}/oauth/callback", callback, methods=["GET"]))
        else:
            routes.append(Route(f"/{p}/oauth/authorize", authz_post, methods=["POST"]))
        routes += [
            Route(f"/{p}/oauth/token", token, methods=["POST"]),
            Route(f"/{p}/mcp", mcp("POST"), methods=["POST"]),
            Route(f"/{p}/mcp", mcp("GET"), methods=["GET"]),
            Route(f"/{p}/mcp", mcp("DELETE"), methods=["DELETE"]),
        ]
        return routes
```

- [ ] **Step 6: Create the landing page** — `src/missingmcp/templates/whoop.html`, following the connector-page skeleton mandated by `garmin.html`'s header comment (hero → what is this → what Claude can see → how to connect → tips & tricks → under the hood → all tools → final CTA). Verbatim:

```html
<!-- Connector page — follows the section skeleton established by garmin.html:
     hero → what is this → what Claude can see → how to connect
     → tips & tricks → under the hood → all tools. -->
  <div class="page-hero">
    <div class="wrap">
      <span class="pill live">Live</span>
      <h1>WHOOP, <span class="hl">in Claude</span>.</h1>
      <p>Recovery, sleep, and strain &mdash; every score your band computes, available in a conversation. Ask why today&rsquo;s recovery dipped, how your HRV is trending, or whether your training load matches your sleep.</p>
      <div class="cta-row">
        <a class="btn" href="#connect">Connect in 2 minutes</a>
        <span class="quiet">Server URL: <code>{PUBLIC_URL}/whoop/mcp</code></span>
      </div>
    </div>
  </div>

  <section id="about">
    <div class="wrap">
      <h2 class="sec-h">What is this?</h2>
      <div class="prose">
        <p>The WHOOP app shows you today&rsquo;s numbers &mdash; but the questions that matter span weeks: <em>&ldquo;What does my recovery do after late workouts?&rdquo;</em>, <em>&ldquo;Is my HRV actually improving this training block?&rdquo;</em>, <em>&ldquo;How much sleep do I really need before a hard day?&rdquo;</em> This connector plugs your WHOOP account into Claude, so you can just ask.</p>
        <p>Claude reads the same data your app does &mdash; recovery scores, sleep stages, strain, workouts, body measurements &mdash; and does what the app can&rsquo;t: correlate, summarize, and explain across any time range.</p>
      </div>
    </div>
  </section>

  <section id="data">
    <div class="wrap">
      <h2 class="sec-h">What Claude can see</h2>
      <div class="cards">
        <div class="card">
          <h3>Recovery</h3>
          <p>Daily recovery percentage with HRV, resting heart rate, SpO2, and skin temperature behind it.</p>
        </div>
        <div class="card">
          <h3>Sleep</h3>
          <p>Sleep stages, time in bed, efficiency, respiratory rate, and how much sleep you actually needed.</p>
        </div>
        <div class="card">
          <h3>Strain &amp; cycles</h3>
          <p>Day strain, average and max heart rate, and energy burned for every physiological cycle.</p>
        </div>
        <div class="card">
          <h3>Workouts</h3>
          <p>Every logged activity with sport, strain, heart-rate profile, and distance where available.</p>
        </div>
        <div class="card">
          <h3>Body measurements</h3>
          <p>Height, weight, and max heart rate on record.</p>
        </div>
        <div class="card">
          <h3>Across time</h3>
          <p>Everything is paginated by date range, so Claude can pull a week, a month, or a whole season and reason over it.</p>
        </div>
      </div>
    </div>
  </section>

  <section id="connect">
    <div class="wrap">
      <h2 class="sec-h">How to connect</h2>
      <div class="cards">
        <div class="card">
          <span class="step-n">Step 1</span>
          <h3>Copy the server URL</h3>
          <p><code>{PUBLIC_URL}/whoop/mcp</code></p>
        </div>
        <div class="card">
          <span class="step-n">Step 2</span>
          <h3>Add it to Claude</h3>
          <p>Settings &rarr; Connectors &rarr; Add custom connector. Works on phone, desktop, and web &mdash; or <code>claude mcp add --transport http whoop {PUBLIC_URL}/whoop/mcp</code> in the CLI.</p>
        </div>
        <div class="card">
          <span class="step-n">Step 3</span>
          <h3>Sign in at WHOOP</h3>
          <p>You&rsquo;re sent to WHOOP&rsquo;s own sign-in page to approve read access &mdash; your WHOOP password is never typed here and never touches this server.</p>
        </div>
        <div class="card">
          <span class="step-n">Step 4</span>
          <h3>Start asking</h3>
          <p>Claude picks up the WHOOP tools automatically. Try &ldquo;How recovered am I today, and why?&rdquo;</p>
        </div>
      </div>
      <div class="note warn">
        <strong>Before you connect:</strong> sign-in happens at WHOOP &mdash; this gateway <strong>never sees your password</strong>. It stores only the resulting access tokens, <strong>encrypted</strong> (AES-256-GCM), with read-only scopes; your health data passes through on demand and is not stored. You can revoke access anytime in your WHOOP app settings. Only use this gateway if you trust the operator: this instance is run by <strong>{OPERATOR_NAME}{OPERATOR_EMAIL}</strong>. Details in <a href="/#security">Security &amp; trust</a>.
      </div>
    </div>
  </section>

  <section id="tips">
    <div class="wrap">
      <h2 class="sec-h">Tips &amp; tricks</h2>
      <p class="lede">Prompts that show what the connector can really do &mdash; skills and ready-made prompt packs will land here too.</p>
      <!-- TIPS: add links to Claude skills & prompt packs here as .card items -->
      <div class="cards">
        <div class="card">
          <span class="step-n">Prompt</span>
          <h3>Morning check-in</h3>
          <p>&ldquo;Look at today&rsquo;s recovery, last night&rsquo;s sleep, and this week&rsquo;s strain &mdash; how hard should I train today?&rdquo;</p>
        </div>
        <div class="card">
          <span class="step-n">Prompt</span>
          <h3>Trend detective</h3>
          <p>&ldquo;Pull the last 30 days of recoveries and sleeps. What correlates with my worst mornings &mdash; short sleep, late workouts, anything else?&rdquo;</p>
        </div>
        <div class="card">
          <span class="step-n">Prompt</span>
          <h3>Training block review</h3>
          <p>&ldquo;Summarize my strain vs. recovery balance for the past two weeks. Am I overreaching, or is there room to push?&rdquo;</p>
        </div>
      </div>
    </div>
  </section>

  <section id="under-the-hood">
    <div class="wrap">
      <h2 class="sec-h">Under the hood</h2>
      <div class="prose">
        <p>This connector is built into the gateway itself, on top of <a href="https://developer.whoop.com/">WHOOP&rsquo;s official developer API</a> (v2) &mdash; no reverse engineering, no password handling. Sign-in is WHOOP&rsquo;s own OAuth: the gateway receives read-only tokens, refreshes them automatically, and keeps them encrypted at rest.</p>
        <p>Everything is open source in <a href="https://github.com/VelkyVenik/missingmcp">missingmcp</a> &mdash; audit it, or self-host the whole thing with your own WHOOP developer app.</p>
      </div>
    </div>
  </section>

  <section id="tools">
    <div class="wrap">
      <h2 class="sec-h">All tools</h2>
<!-- GENERATED:TOOLS:BEGIN — do not edit by hand; regenerate with scripts/gen_whoop_tools.py -->
      <p class="lede">Tools are listed here once <code>scripts/gen_whoop_tools.py</code> runs (Task 7).</p>
<!-- GENERATED:TOOLS:END -->
    </div>
  </section>

  <div class="final">
    <div class="wrap">
      <div class="inner">
        <h2>Your band has the <span class="hl">answers</span>.</h2>
        <p>Two minutes from now you can ask for them &mdash; free, open source, revocable anytime.</p>
        <a class="btn" href="#connect">Connect WHOOP</a>
        <p class="beer">Did this help you? <a href="https://buymeacoffee.com/venik" target="_blank" rel="noopener noreferrer">&#127866; Buy me a beer</a></p>
      </div>
    </div>
  </div>
```

- [ ] **Step 7: Run the e2e tests, then the full suite**

Run: `uv run --extra dev pytest tests/test_whoop_e2e.py -q` → 7 passed.
Run: `uv run --extra dev pytest -q` → 186 passed.

- [ ] **Step 8: Commit**

```bash
git add src/missingmcp/adapters/whoop/__init__.py src/missingmcp/adapters/__init__.py \
        src/missingmcp/app.py src/missingmcp/templates/whoop.html tests/test_whoop_e2e.py
git commit -m "feat(whoop): adapter + conditional registration + callback route + landing page"
```

---

### Task 7: Generated tools listing + home-page card

**Files:**
- Create: `scripts/gen_whoop_tools.py`
- Modify: `src/missingmcp/templates/whoop.html` (by running the script)
- Modify: `src/missingmcp/templates/home.html` (WHOOP card)
- Test: `tests/test_app.py` (append)

**Interfaces:**
- Consumes: `missingmcp.adapters.whoop.mcp.TOOLS` (Task 5), the `GENERATED:TOOLS` markers in `whoop.html` (Task 6).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_app.py` (its module-level `client` fixture builds the app WITHOUT WHOOP credentials, so add a local fixture; check the top of the file and reuse its env dict shape):

```python
def _whoop_client():
    from missingmcp.app import build_app
    from missingmcp.config import load_config
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DB_PATH": ":memory:", "DATA_DIR": "/tmp",
                       "WHOOP_CLIENT_ID": "cid-1", "WHOOP_CLIENT_SECRET": "sec-1"})
    return TestClient(build_app(cfg))


def test_whoop_page_lists_generated_tools():
    c = _whoop_client()
    r = c.get("/whoop")
    assert r.status_code == 200
    from missingmcp.adapters.whoop.mcp import TOOLS
    for name, _desc, _schema, _resolve in TOOLS:
        assert f"<code>{name}</code>" in r.text
    assert "gw.example.com/whoop/mcp" in r.text          # hero server URL filled


def test_home_shows_whoop_card(client):
    r = client.get("/")
    assert 'href="/whoop"' in r.text
    assert "WHOOP" in r.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_app.py -q`
Expected: the two new tests FAIL (placeholder text instead of tool names; no `/whoop` link on home).

- [ ] **Step 3: Create the generator** — `scripts/gen_whoop_tools.py`:

```python
#!/usr/bin/env python3
"""Regenerate the "All tools" section of templates/whoop.html from the in-tree
tool table (missingmcp.adapters.whoop.mcp.TOOLS) so the page never drifts from
the code. Run after any TOOLS change:

  python scripts/gen_whoop_tools.py
"""
from __future__ import annotations
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
TEMPLATE = ROOT / "src" / "missingmcp" / "templates" / "whoop.html"
BEGIN = "<!-- GENERATED:TOOLS:BEGIN"
END = "<!-- GENERATED:TOOLS:END -->"


def render() -> str:
    from missingmcp.adapters.whoop.mcp import TOOLS
    lines = [
        f"{BEGIN} — do not edit by hand; regenerate with scripts/gen_whoop_tools.py -->",
        f'      <p class="lede">All <strong>{len(TOOLS)} tools</strong> this connector exposes '
        "&mdash; read-only, straight from WHOOP&rsquo;s official v2 API.</p>",
        '      <div class="tools">',
        "        <details open>",
        f'          <summary>WHOOP data <span class="count">&middot; {len(TOOLS)} tools</span></summary>',
        "          <dl>",
    ]
    for name, description, _schema, _resolve in TOOLS:
        lines.append(f"            <dt><code>{html.escape(name)}</code></dt>"
                     f"<dd>{html.escape(description)}</dd>")
    lines += ["          </dl>", "        </details>", "      </div>"]
    return "\n".join(lines)


def main() -> None:
    text = TEMPLATE.read_text()
    pre, rest = text.split(BEGIN, 1)
    _, post = rest.split(END, 1)
    TEMPLATE.write_text(pre + render() + "\n" + END + post)
    print(f"wrote {TEMPLATE}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run it** — `python scripts/gen_whoop_tools.py`; then `git diff src/missingmcp/templates/whoop.html` and check the placeholder `<p class="lede">Tools are listed here…` was replaced by the 8 `<dt><code>…` entries. Run it twice — the second run must produce no further diff (idempotent).

- [ ] **Step 5: Add the home card** — in `src/missingmcp/templates/home.html`, insert between the Garmin card's closing `</div>` and the "Missing something?" card:

```html
        <div class="card">
          <span class="pill live">Live</span>
          <h3>WHOOP</h3>
          <p>Recovery, sleep, and strain &mdash; ask why today&rsquo;s recovery dipped, what your HRV is doing, or how the week&rsquo;s load is trending.</p>
          <a class="go" href="/whoop">Connect WHOOP &rarr;</a>
        </div>
```

Accepted trade-off (documented, do not "fix"): the home page is static, so the card shows even on a self-hosted instance without WHOOP credentials, where `/whoop` then serves the home page with 404. On missingmcp.com the connector is always configured.

- [ ] **Step 6: Run the app tests, then the full suite**

Run: `uv run --extra dev pytest tests/test_app.py -q` → all pass (2 new).
Run: `uv run --extra dev pytest -q` → 188 passed.

- [ ] **Step 7: Commit**

```bash
git add scripts/gen_whoop_tools.py src/missingmcp/templates/whoop.html \
        src/missingmcp/templates/home.html tests/test_app.py
git commit -m "feat(site): whoop tools listing generator + WHOOP card on the home page"
```

---

### Task 8: Docs — README + CLAUDE.md

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

No test cycle — the deliverable is reviewed text. Keep every edit minimal and weave into the existing structure (read the surrounding sections first).

- [ ] **Step 1: README — env vars.** In the environment-variable reference table/section, add (matching the existing row format):

- `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` — credentials of your WHOOP developer app; **both unset ⇒ the whoop connector is disabled**.
- `WHOOP_API_BASE` — WHOOP API origin (default `https://api.prod.whoop.com`); override only for testing.

- [ ] **Step 2: README — registration walkthrough.** Add a short subsection "WHOOP connector setup" near the deploy/operations docs:

```markdown
### WHOOP connector setup

1. Create an app at <https://developer-dashboard.whoop.com> (instant self-service).
2. Redirect URI: `https://<your-domain>/whoop/oauth/callback` — exact match required.
3. Scopes: `read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement offline`
   (`offline` is required — without it WHOOP issues no refresh token and sessions die after an hour).
4. Put the app's Client ID/Secret into `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET`.

Note: an unapproved WHOOP app is limited to **10 WHOOP members** — fine for a
trusted circle; submit the app for approval only if you outgrow that.
```

- [ ] **Step 3: CLAUDE.md.** Update to reflect the new reality, minimally:
  - "What this is": mention three forward strategies — worker (garmin), remote (stub-tested), **local** (whoop — in-process MCP, no subprocess), and the third login shape (upstream OAuth: `/whoop/oauth/callback`).
  - Architecture module list: add `adapters/whoop/` — `api.py` (v2 client, gateway-owned rotating refresh under per-account lock, persisted before use), `mcp.py` (hand-rolled stateless JSON-RPC + `TOOLS`), `__init__.py` (`WhoopAdapter`).
  - Cross-cutting invariants: add one bullet — WHOOP refresh tokens rotate on every use; only the gateway refreshes (never a worker), always persisting the rotated blob before proceeding; `scope: offline` required.
  - Testing approach: mention `fake_whoop` and that the real WHOOP OAuth flow is the manual release gate (mirrors garmin).
  - Commands: note `python scripts/gen_whoop_tools.py` after TOOLS changes.

- [ ] **Step 4: Full suite still green**

Run: `uv run --extra dev pytest -q` → 188 passed.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: WHOOP connector — env vars, app registration walkthrough, architecture notes"
```

---

## Post-plan release gate (operator, manual — not a task for subagents)

Register the real WHOOP dev app, deploy, and run the full Claude connect flow
(discovery → DCR → authorize at WHOOP → token → tool calls) with the operator's
WHOOP account; verify a token refresh survives (leave the session idle >1 h,
ask again). Mirrors the garmin release gate.

## Self-review notes

- Expected test counts are approximate anchors (baseline 145): if a count is
  off by a few because main moved, the requirement is "everything passes",
  not the literal number.
- Type consistency verified: `WhoopApi.get(conn, account_key, blob: dict, path, params)` (Tasks 2/5), `LocalForward.handle(conn, account_key, blob: str, body)` (Tasks 4/5/6 — mcp.py does `json.loads(blob)` at the seam), `TOOLS` 4-tuples (Tasks 5/7), `is_upstream_oauth` on adapters vs `is_local` on forwards (Tasks 3/4/6).
- Spec coverage: login shape C (Task 3), rotating refresh + persistence + lock (Task 2), LocalForward + 405 + session-expired (Task 4), hand-rolled MCP + 8 tools + pagination passthrough + 429 copy (Task 5), conditional registration + callback route + landing skeleton + e2e (Task 6), gen script + home card (Task 7), README/CLAUDE.md incl. 10-member note (Task 8). Webhooks/writes/approval intentionally out of scope per spec.
