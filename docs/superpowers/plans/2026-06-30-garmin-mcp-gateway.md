# Garmin MCP Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A multi-user, OAuth 2.1–protected remote MCP server that lets a small trusted circle each connect their own Garmin account from Claude mobile/desktop/web, by wrapping the **unmodified** `garmin_mcp` worker.

**Architecture:** A Python/Starlette gateway terminates OAuth (DCR + Authorization Code + PKCE), performs the Garmin login via `garminconnect` (discarding the password, keeping only tokens), stores per-account encrypted tokens in SQLite, and for each account spawns and reverse-proxies to a per-user `garmin-mcp` subprocess running in `streamable-http` mode on `127.0.0.1`. nginx (operator-managed) terminates TLS in front.

**Tech Stack:** Python 3.12, Starlette + Uvicorn, httpx (async, streaming), garminconnect (Garmin login + tokens), cryptography (AES-256-GCM), SQLite (`sqlite3` stdlib), pytest + pytest-asyncio. Packaged with `uv`. Deployed via Docker Compose.

## Global Constraints

- **Do NOT modify `garmin_mcp`.** Interact with it only via its documented CLI entrypoint (`garmin-mcp`) and env vars (`GARMIN_MCP_TRANSPORT`, `GARMIN_MCP_HOST`, `GARMIN_MCP_PORT`, `GARMINTOKENS`). No source edits, no importing its internal modules.
- **`garmin_mcp` is pinned** to a specific git commit via `GARMIN_MCP_REF` (supply-chain hygiene).
- **Garmin password is never persisted** — held in memory only during login, then discarded.
- **Secrets never appear in logs** — no password, token, or MFA code; at most an 8-char token-hash prefix.
- **Workers bind `127.0.0.1` only.**
- **PKCE S256 is required**; `plain` is rejected.
- **Encryption:** AES-256-GCM, key = `SHA-256(GATEWAY_SECRET)`, random 12-byte nonce, stored as `nonce_hex:ciphertext_hex`. `GATEWAY_SECRET` must be ≥32 chars; the process refuses to start otherwise.
- **Garmin tokens are valid ~6 months** (per `garmin_mcp/auth_cli.py`); the design doc's "~yearly" is superseded by this value. Re-auth = repeat the OAuth login.
- **Python 3.12** (matches the `garmin_mcp` worker's required interpreter).
- All source lives under `src/garmin_gateway/`; all tests under `tests/`.

---

## File Structure

```
garmin-mcp-gateway/
├── pyproject.toml                     # deps + console script + pytest config
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── nginx.conf.example
├── README.md
├── src/garmin_gateway/
│   ├── __init__.py
│   ├── config.py        # Config dataclass + load_config()
│   ├── log.py           # structured JSON logging, secret-free
│   ├── store.py         # SQLite schema, AES-GCM crypto, token hashing, CRUD
│   ├── security.py      # PKCE, redirect_uri allowlist, CSRF, rate limiter, headers, body limit
│   ├── garmin_login.py  # garminconnect login + MFA resume + token dump/verify
│   ├── oauth.py         # metadata, DCR, authorize (GET/POST + MFA), token exchange
│   ├── workers.py       # WorkerManager: spawn/ensure/idle-reaper/cap
│   ├── proxy.py         # /mcp POST/GET/DELETE forwarding to the user's worker
│   ├── app.py           # Starlette app: routes, middleware, lifespan
│   └── templates/
│       ├── landing.html
│       ├── authorize.html
│       └── mfa.html
└── tests/
    ├── conftest.py      # fixtures: tmp config, in-memory/temp DB, fake worker server
    ├── test_store.py
    ├── test_security.py
    ├── test_garmin_login.py
    ├── test_oauth.py
    ├── test_workers.py
    └── test_proxy.py
```

Each `oauth.py` concern (metadata/DCR, authorize, token) is delivered by a separate task but lives in one cohesive module, because they share request-parsing helpers and the same store.

---

### Task 1: Project scaffold, config, logging

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/garmin_gateway/__init__.py`
- Create: `src/garmin_gateway/config.py`
- Create: `src/garmin_gateway/log.py`
- Test: `tests/test_config.py` (created here, lives at repo root `tests/`)

**Interfaces:**
- Produces: `garmin_gateway.config.Config` (frozen dataclass, fields below) and `load_config(env: Mapping[str,str] | None = None) -> Config`. Raises `ValueError` if `GATEWAY_SECRET` is missing or <32 chars.
- Produces: `garmin_gateway.log.log(event: str, **fields)`, `log_warn(...)`, `log_error(...)` — emit one JSON line to stdout; values are stringified; callers must never pass secrets.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "garmin-mcp-gateway"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "starlette>=0.40",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "garminconnect>=0.3.2",
    "cryptography>=42",
    "python-multipart>=0.0.9",
]

[project.scripts]
garmin-gateway = "garmin_gateway.app:main"

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/garmin_gateway"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
data/
*.db
*.db-wal
*.db-shm
.pytest_cache/
```

- [ ] **Step 3: Create `src/garmin_gateway/__init__.py`** (empty file).

- [ ] **Step 4: Write the failing test** `tests/test_config.py`

```python
import pytest
from garmin_gateway.config import load_config

BASE = {"GATEWAY_SECRET": "x" * 32, "PUBLIC_URL": "https://gw.example.com"}

def test_loads_defaults():
    c = load_config(BASE)
    assert c.public_url == "https://gw.example.com"
    assert c.port == 8080
    assert c.worker_port_start == 9000
    assert c.worker_idle_ttl == 900
    assert c.garmin_mcp_cmd == ["garmin-mcp"]

def test_strips_trailing_slash_from_public_url():
    c = load_config({**BASE, "PUBLIC_URL": "https://gw.example.com/"})
    assert c.public_url == "https://gw.example.com"

def test_rejects_short_secret():
    with pytest.raises(ValueError):
        load_config({"GATEWAY_SECRET": "short"})

def test_rejects_missing_secret():
    with pytest.raises(ValueError):
        load_config({})
```

- [ ] **Step 5: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: garmin_gateway.config`).

- [ ] **Step 6: Implement `src/garmin_gateway/config.py`**

```python
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Config:
    gateway_secret: str
    public_url: str
    port: int
    data_dir: str
    db_path: str
    garmin_mcp_cmd: list[str]
    worker_port_start: int
    worker_port_end: int
    worker_idle_ttl: int          # seconds
    worker_startup_timeout: int   # seconds
    max_workers: int
    operator_name: str
    operator_email: str


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    secret = env.get("GATEWAY_SECRET", "")
    if len(secret) < 32:
        raise ValueError("GATEWAY_SECRET must be set and at least 32 characters")
    data_dir = env.get("DATA_DIR", "/data")
    public_url = env.get("PUBLIC_URL", "http://localhost:8080").rstrip("/")
    cmd = env.get("GARMIN_MCP_CMD", "garmin-mcp").split()
    return Config(
        gateway_secret=secret,
        public_url=public_url,
        port=int(env.get("PORT", "8080")),
        data_dir=data_dir,
        db_path=env.get("DB_PATH", os.path.join(data_dir, "gateway.db")),
        garmin_mcp_cmd=cmd,
        worker_port_start=int(env.get("WORKER_PORT_START", "9000")),
        worker_port_end=int(env.get("WORKER_PORT_END", "9099")),
        worker_idle_ttl=int(env.get("WORKER_IDLE_TTL", "900")),
        worker_startup_timeout=int(env.get("WORKER_STARTUP_TIMEOUT", "20")),
        max_workers=int(env.get("MAX_WORKERS", "10")),
        operator_name=env.get("OPERATOR_NAME", "the operator"),
        operator_email=env.get("OPERATOR_EMAIL", ""),
    )
```

- [ ] **Step 7: Implement `src/garmin_gateway/log.py`**

```python
from __future__ import annotations
import json
import sys
from typing import Any


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    record = {"level": level, "event": event}
    for k, v in fields.items():
        record[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def log(event: str, **fields: Any) -> None:
    _emit("info", event, fields)


def log_warn(event: str, **fields: Any) -> None:
    _emit("warn", event, fields)


def log_error(event: str, **fields: Any) -> None:
    _emit("error", event, fields)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (4 passed).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore src/garmin_gateway/__init__.py src/garmin_gateway/config.py src/garmin_gateway/log.py tests/test_config.py
git commit -m "feat: project scaffold, config loader, structured logging"
```

---

### Task 2: Store — crypto, schema, CRUD

**Files:**
- Create: `src/garmin_gateway/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `init_db(db_path: str) -> sqlite3.Connection` — creates tables, sets `PRAGMA journal_mode=WAL`, chmods the db file `0600`. Pass `":memory:"` in tests.
  - `encrypt(secret: str, plaintext: str) -> str` / `decrypt(secret: str, blob: str) -> str`
  - `hash_token(token: str) -> str` (sha256 hex)
  - `upsert_account(conn, key: str, tokens_json: str, secret: str) -> None`
  - `get_account_tokens(conn, key: str, secret: str) -> str | None`
  - `list_accounts(conn) -> list[dict]` (keys: `garmin_user_key`, `created_at`, `updated_at`)
  - `create_access_token(conn, token_hash: str, key: str, client_id: str) -> None`
  - `account_key_for_token_hash(conn, token_hash: str) -> str | None` (also bumps `last_used`)
  - `create_client(conn, client_id, client_secret_hash, redirect_uris: list[str], client_name: str | None) -> None`
  - `get_client(conn, client_id) -> dict | None` (keys: `client_secret_hash`, `redirect_uris: list[str]`)
  - `create_code(conn, code_hash, client_id, redirect_uri, code_challenge, method, key, ttl=600) -> None`
  - `consume_code(conn, code_hash) -> dict | None` (one-time; keys: `client_id`, `redirect_uri`, `code_challenge`, `code_challenge_method`, `garmin_user_key`; returns `None` if missing/expired)
  - `cleanup_expired_codes(conn) -> None`

- [ ] **Step 1: Write the failing test** `tests/test_store.py`

```python
import time
import pytest
from garmin_gateway import store

SECRET = "k" * 40


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


def test_crypto_roundtrip():
    blob = store.encrypt(SECRET, "hello-tokens")
    assert blob != "hello-tokens"
    assert store.decrypt(SECRET, blob) == "hello-tokens"


def test_decrypt_with_wrong_secret_fails():
    blob = store.encrypt(SECRET, "secret")
    with pytest.raises(Exception):
        store.decrypt("w" * 40, blob)


def test_hash_token_is_stable_and_hex():
    h = store.hash_token("abc")
    assert h == store.hash_token("abc")
    assert len(h) == 64


def test_account_upsert_and_fetch(conn):
    store.upsert_account(conn, "me@x.cz", '{"t":1}', SECRET)
    assert store.get_account_tokens(conn, "me@x.cz", SECRET) == '{"t":1}'
    store.upsert_account(conn, "me@x.cz", '{"t":2}', SECRET)
    assert store.get_account_tokens(conn, "me@x.cz", SECRET) == '{"t":2}'
    assert store.get_account_tokens(conn, "absent@x.cz", SECRET) is None


def test_access_token_maps_to_account(conn):
    store.upsert_account(conn, "me@x.cz", "{}", SECRET)
    store.create_access_token(conn, "hash1", "me@x.cz", "client1")
    assert store.account_key_for_token_hash(conn, "hash1") == "me@x.cz"
    assert store.account_key_for_token_hash(conn, "nope") is None


def test_client_roundtrip(conn):
    store.create_client(conn, "c1", "secret_hash", ["https://a/cb", "https://b/cb"], "Claude")
    c = store.get_client(conn, "c1")
    assert c["client_secret_hash"] == "secret_hash"
    assert c["redirect_uris"] == ["https://a/cb", "https://b/cb"]


def test_code_is_one_time(conn):
    store.create_code(conn, "ch", "c1", "https://a/cb", "challenge", "S256", "me@x.cz")
    row = store.consume_code(conn, "ch")
    assert row["garmin_user_key"] == "me@x.cz"
    assert row["code_challenge"] == "challenge"
    assert store.consume_code(conn, "ch") is None  # already consumed


def test_expired_code_returns_none(conn):
    store.create_code(conn, "ch", "c1", "https://a/cb", "x", "S256", "me@x.cz", ttl=-1)
    assert store.consume_code(conn, "ch") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL (`ModuleNotFoundError: garmin_gateway.store`).

- [ ] **Step 3: Implement `src/garmin_gateway/store.py`**

```python
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# --- crypto ---------------------------------------------------------------

def _key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def encrypt(secret: str, plaintext: str) -> str:
    aes = AESGCM(_key(secret))
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode(), None)
    return nonce.hex() + ":" + ct.hex()


def decrypt(secret: str, blob: str) -> str:
    nonce_hex, ct_hex = blob.split(":", 1)
    aes = AESGCM(_key(secret))
    pt = aes.decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None)
    return pt.decode()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- schema ---------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS garmin_accounts (
            garmin_user_key   TEXT PRIMARY KEY,
            garmin_tokens_enc TEXT NOT NULL,
            created_at        TEXT DEFAULT (datetime('now')),
            updated_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS access_tokens (
            token_hash      TEXT PRIMARY KEY,
            garmin_user_key TEXT NOT NULL,
            client_id       TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            last_used       TEXT
        );
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id          TEXT PRIMARY KEY,
            client_secret_hash TEXT NOT NULL,
            redirect_uris      TEXT NOT NULL,
            client_name        TEXT,
            created_at         TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS oauth_codes (
            code_hash             TEXT PRIMARY KEY,
            client_id             TEXT NOT NULL,
            redirect_uri          TEXT NOT NULL,
            code_challenge        TEXT,
            code_challenge_method TEXT,
            garmin_user_key       TEXT NOT NULL,
            expires_at            INTEGER NOT NULL,
            created_at            TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    if db_path not in (":memory:", ""):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(db_path + suffix, 0o600)
            except OSError:
                pass
    return conn


# --- accounts -------------------------------------------------------------

def upsert_account(conn, key: str, tokens_json: str, secret: str) -> None:
    enc = encrypt(secret, tokens_json)
    conn.execute(
        """
        INSERT INTO garmin_accounts (garmin_user_key, garmin_tokens_enc)
        VALUES (?, ?)
        ON CONFLICT(garmin_user_key)
        DO UPDATE SET garmin_tokens_enc=excluded.garmin_tokens_enc,
                      updated_at=datetime('now')
        """,
        (key, enc),
    )
    conn.commit()


def get_account_tokens(conn, key: str, secret: str) -> str | None:
    row = conn.execute(
        "SELECT garmin_tokens_enc FROM garmin_accounts WHERE garmin_user_key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return decrypt(secret, row["garmin_tokens_enc"])


def list_accounts(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT garmin_user_key, created_at, updated_at FROM garmin_accounts ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


# --- access tokens --------------------------------------------------------

def create_access_token(conn, token_hash: str, key: str, client_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO access_tokens (token_hash, garmin_user_key, client_id, last_used) "
        "VALUES (?, ?, ?, datetime('now'))",
        (token_hash, key, client_id),
    )
    conn.commit()


def account_key_for_token_hash(conn, token_hash: str) -> str | None:
    row = conn.execute(
        "SELECT garmin_user_key FROM access_tokens WHERE token_hash=?", (token_hash,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE access_tokens SET last_used=datetime('now') WHERE token_hash=?",
        (token_hash,),
    )
    conn.commit()
    return row["garmin_user_key"]


# --- oauth clients --------------------------------------------------------

def create_client(conn, client_id, client_secret_hash, redirect_uris: list[str], client_name) -> None:
    conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret_hash, redirect_uris, client_name) "
        "VALUES (?, ?, ?, ?)",
        (client_id, client_secret_hash, json.dumps(redirect_uris), client_name),
    )
    conn.commit()


def get_client(conn, client_id) -> dict | None:
    row = conn.execute(
        "SELECT client_secret_hash, redirect_uris FROM oauth_clients WHERE client_id=?",
        (client_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "client_secret_hash": row["client_secret_hash"],
        "redirect_uris": json.loads(row["redirect_uris"]),
    }


# --- oauth codes ----------------------------------------------------------

def create_code(conn, code_hash, client_id, redirect_uri, code_challenge, method, key, ttl=600) -> None:
    conn.execute(
        "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, code_challenge, "
        "code_challenge_method, garmin_user_key, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code_hash, client_id, redirect_uri, code_challenge, method, key, int(time.time()) + ttl),
    )
    conn.commit()


def consume_code(conn, code_hash) -> dict | None:
    row = conn.execute(
        "SELECT client_id, redirect_uri, code_challenge, code_challenge_method, "
        "garmin_user_key, expires_at FROM oauth_codes WHERE code_hash=?",
        (code_hash,),
    ).fetchone()
    conn.execute("DELETE FROM oauth_codes WHERE code_hash=?", (code_hash,))
    conn.commit()
    if row is None or time.time() > row["expires_at"]:
        return None
    return {
        "client_id": row["client_id"],
        "redirect_uri": row["redirect_uri"],
        "code_challenge": row["code_challenge"],
        "code_challenge_method": row["code_challenge_method"],
        "garmin_user_key": row["garmin_user_key"],
    }


def cleanup_expired_codes(conn) -> None:
    conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (int(time.time()),))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/store.py tests/test_store.py
git commit -m "feat: SQLite store with AES-256-GCM encryption and OAuth tables"
```

---

### Task 3: Security helpers

**Files:**
- Create: `src/garmin_gateway/security.py`
- Test: `tests/test_security.py`

**Interfaces:**
- Produces:
  - `security_headers() -> dict[str, str]`
  - `verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool` (only `S256` accepted; `plain`/other → `False`)
  - `validate_redirect_uri(uri: str, allowed: list[str]) -> bool` (exact match)
  - `new_secret(nbytes: int = 32) -> str` (URL-safe token)
  - `validate_session_id(sid: str) -> bool` (allow `[A-Za-z0-9._-]{1,128}`)
  - `class RateLimiter: __init__(self, clock=time.monotonic); check(self, key: str, limit: int, window: float) -> bool`
  - `class CsrfStore: __init__(self, ttl=600, clock=time.monotonic); issue(self) -> str; consume(self, token: str) -> bool`
  - `async read_body_limited(request, max_bytes: int = 1_048_576) -> bytes | None` (returns `None` if over limit)

- [ ] **Step 1: Write the failing test** `tests/test_security.py`

```python
import base64, hashlib
from garmin_gateway import security


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def test_pkce_s256_ok():
    v = "verifier-123"
    assert security.verify_pkce(v, _challenge(v), "S256")


def test_pkce_wrong_verifier_fails():
    assert not security.verify_pkce("nope", _challenge("verifier-123"), "S256")


def test_pkce_plain_rejected():
    assert not security.verify_pkce("v", "v", "plain")


def test_redirect_uri_allowlist():
    allowed = ["https://claude.ai/cb"]
    assert security.validate_redirect_uri("https://claude.ai/cb", allowed)
    assert not security.validate_redirect_uri("https://evil.com/cb", allowed)


def test_session_id_validation():
    assert security.validate_session_id("abc-123_.A")
    assert not security.validate_session_id("bad id space")
    assert not security.validate_session_id("")


def test_rate_limiter_blocks_over_limit():
    clock = [0.0]
    rl = security.RateLimiter(clock=lambda: clock[0])
    assert rl.check("ip", limit=2, window=60)
    assert rl.check("ip", limit=2, window=60)
    assert not rl.check("ip", limit=2, window=60)
    clock[0] = 61
    assert rl.check("ip", limit=2, window=60)  # window slid


def test_csrf_one_time():
    clock = [0.0]
    cs = security.CsrfStore(ttl=600, clock=lambda: clock[0])
    tok = cs.issue()
    assert cs.consume(tok)
    assert not cs.consume(tok)         # one-time
    assert not cs.consume("forged")


def test_csrf_expires():
    clock = [0.0]
    cs = security.CsrfStore(ttl=10, clock=lambda: clock[0])
    tok = cs.issue()
    clock[0] = 11
    assert not cs.consume(tok)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_security.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/garmin_gateway/security.py`**

```python
from __future__ import annotations
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Callable

_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method != "S256" or not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(expected, code_challenge)


def validate_redirect_uri(uri: str, allowed: list[str]) -> bool:
    return uri in allowed


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def validate_session_id(sid: str) -> bool:
    return bool(_SESSION_RE.match(sid))


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float) -> bool:
        now = self._clock()
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


class CsrfStore:
    def __init__(self, ttl: float = 600, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._tokens: dict[str, float] = {}

    def issue(self) -> str:
        self._gc()
        tok = secrets.token_urlsafe(24)
        self._tokens[tok] = self._clock()
        return tok

    def consume(self, token: str) -> bool:
        self._gc()
        return self._tokens.pop(token, None) is not None

    def _gc(self) -> None:
        now = self._clock()
        for t, ts in list(self._tokens.items()):
            if now - ts > self._ttl:
                self._tokens.pop(t, None)


async def read_body_limited(request, max_bytes: int = 1_048_576) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_security.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/security.py tests/test_security.py
git commit -m "feat: security helpers (PKCE, CSRF, rate limiter, headers)"
```

---

### Task 4: Garmin login wrapper

**Files:**
- Create: `src/garmin_gateway/garmin_login.py`
- Test: `tests/test_garmin_login.py`

**Interfaces:**
- Produces:
  - `@dataclass LoginResult: status: str  # "ok" | "needs_mfa"`, `tokens_json: str | None`, `pending: object | None`
  - `start_login(email: str, password: str) -> LoginResult` — wraps `Garmin(..., return_on_mfa=True).login()`. On `needs_mfa`, `pending` holds the opaque `(client, state)` tuple; the **caller discards the password** after this returns.
  - `resume_login(pending, mfa_code: str) -> str` — returns `tokens_json`.
  - `verify_tokens(tokens_json: str) -> str` — independently confirms the tokens authenticate (fresh token login), returns the Garmin display name; raises `GarminLoginError` on failure.
  - `class GarminLoginError(Exception)`
- Consumes: `garminconnect.Garmin` (mocked in tests).
- Note: token capture mirrors `garmin_mcp/auth_cli.py` exactly — `client.dump(dir)` then read `garmin_tokens.json`.

- [ ] **Step 1: Write the failing test** `tests/test_garmin_login.py`

```python
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from garmin_gateway import garmin_login


def _fake_garmin_factory(needs_mfa=False, dump_payload='{"oauth":"tok"}'):
    """Return a fake Garmin class whose .dump writes garmin_tokens.json."""
    def dump(path):
        with open(os.path.join(path, "garmin_tokens.json"), "w") as f:
            f.write(dump_payload)

    def make(*args, **kwargs):
        g = MagicMock()
        g.client.dump.side_effect = dump
        if needs_mfa and (kwargs.get("password") or len(args) >= 2):
            g.login.return_value = ("needs_mfa", "STATE")
        else:
            g.login.return_value = (None, None)
        g.get_full_name.return_value = "Vaclav S"
        return g
    return make


def test_login_no_mfa_returns_tokens():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        r = garmin_login.start_login("me@x.cz", "pw")
    assert r.status == "ok"
    assert json.loads(r.tokens_json) == {"oauth": "tok"}


def test_login_needs_mfa_then_resume():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory(needs_mfa=True)):
        r = garmin_login.start_login("me@x.cz", "pw")
        assert r.status == "needs_mfa"
        assert r.tokens_json is None
        tokens = garmin_login.resume_login(r.pending, "123456")
    assert json.loads(tokens) == {"oauth": "tok"}


def test_verify_tokens_returns_name():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        name = garmin_login.verify_tokens('{"oauth":"tok"}')
    assert name == "Vaclav S"


def test_verify_tokens_raises_when_no_profile():
    def make(*a, **k):
        g = MagicMock()
        g.get_full_name.return_value = None
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError):
            garmin_login.verify_tokens('{"oauth":"tok"}')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_garmin_login.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/garmin_gateway/garmin_login.py`**

```python
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass
from garminconnect import Garmin


class GarminLoginError(Exception):
    pass


@dataclass
class LoginResult:
    status: str                 # "ok" | "needs_mfa"
    tokens_json: str | None = None
    pending: object | None = None


def _dump_tokens(client) -> str:
    """Mirror garmin_mcp/auth_cli.py: dump to a dir, read garmin_tokens.json."""
    with tempfile.TemporaryDirectory() as d:
        client.dump(d)
        with open(os.path.join(d, "garmin_tokens.json")) as f:
            return f.read()


def start_login(email: str, password: str) -> LoginResult:
    g = Garmin(email=email, password=password, return_on_mfa=True)
    result1, result2 = g.login()
    if result1 == "needs_mfa":
        return LoginResult(status="needs_mfa", pending=(g, result2))
    return LoginResult(status="ok", tokens_json=_dump_tokens(g.client))


def resume_login(pending, mfa_code: str) -> str:
    client, state = pending
    client.resume_login(state, mfa_code)
    return _dump_tokens(client.client)


def verify_tokens(tokens_json: str) -> str:
    """Confirm tokens authenticate via a fresh token login; return display name."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "garmin_tokens.json"), "w") as f:
            f.write(tokens_json)
        try:
            g = Garmin()
            g.login(d)
            name = g.get_full_name()
        except Exception as e:  # noqa: BLE001 - surface as our error type
            raise GarminLoginError(str(e).split(":")[0].strip() or e.__class__.__name__)
    if not name:
        raise GarminLoginError("session is not authenticated (no profile returned)")
    return name
```

> **Note on `resume_login`:** `start_login` stores the high-level `Garmin` object as `pending[0]`; in `_dump_tokens` we call `client.client.dump(...)` where the outer `client` is the `Garmin` instance and `.client` is its garth client — matching `garmin_mcp`'s `garmin.client.dump(...)`. The test's fake sets `g.client.dump`, so `_dump_tokens(g.client)` ⇒ in `start_login` we pass `g.client`, and in `resume_login` `client` is the `Garmin` (`pending[0]`) so we pass `client.client`. Keep both call sites passing the garth client (`g.client`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_garmin_login.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/garmin_login.py tests/test_garmin_login.py
git commit -m "feat: Garmin login wrapper (MFA-aware, token capture + verify)"
```

---

### Task 5: OAuth metadata + Dynamic Client Registration

**Files:**
- Create: `src/garmin_gateway/oauth.py`
- Test: `tests/test_oauth.py` (this task adds the metadata + register tests; later tasks extend the same file)

**Interfaces:**
- Produces (in `oauth.py`):
  - `metadata(config) -> dict` — OAuth Authorization Server Metadata (RFC 8414) JSON body.
  - `async register_client(request, conn) -> JSONResponse` — RFC 7591 Dynamic Client Registration. Validates `redirect_uris` (list of non-empty strings). Returns `{client_id, client_secret, redirect_uris, token_endpoint_auth_method: "client_secret_post"}`. Stores `hash_token(client_secret)`.
- Consumes: `store`, `security.new_secret`, `security.read_body_limited`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_oauth.py`)

```python
import json
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, oauth
from garmin_gateway.config import load_config

CONFIG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com"})


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


def test_metadata_shape():
    m = oauth.metadata(CONFIG)
    assert m["issuer"] == "https://gw.example.com"
    assert m["authorization_endpoint"] == "https://gw.example.com/oauth/authorize"
    assert m["token_endpoint"] == "https://gw.example.com/oauth/token"
    assert m["registration_endpoint"] == "https://gw.example.com/oauth/register"
    assert "S256" in m["code_challenge_methods_supported"]


def _client_app(conn):
    async def reg(request):
        return await oauth.register_client(request, conn)
    return TestClient(Starlette(routes=[Route("/oauth/register", reg, methods=["POST"])]))


def test_register_returns_client_id(conn):
    c = _client_app(conn)
    resp = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"]
    assert body["client_secret"]
    assert body["redirect_uris"] == ["https://claude.ai/cb"]
    assert store.get_client(conn, body["client_id"]) is not None


def test_register_rejects_missing_redirect_uris(conn):
    c = _client_app(conn)
    resp = c.post("/oauth/register", json={})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: FAIL (`ModuleNotFoundError` / missing attrs).

- [ ] **Step 3: Implement metadata + register in `src/garmin_gateway/oauth.py`**

```python
from __future__ import annotations
import json
from starlette.responses import JSONResponse
from . import store, security


def metadata(config) -> dict:
    base = config.public_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


async def register_client(request, conn) -> JSONResponse:
    body = await security.read_body_limited(request)
    if body is None:
        return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    client_id = security.new_secret(16)
    client_secret = security.new_secret(32)
    store.create_client(conn, client_id, store.hash_token(client_secret), uris, data.get("client_name"))
    return JSONResponse(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "client_secret_post",
        },
        status_code=201,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/oauth.py tests/test_oauth.py
git commit -m "feat: OAuth metadata + dynamic client registration"
```

---

### Task 6: OAuth authorize (login form + Garmin login + MFA) and templates

**Files:**
- Create: `src/garmin_gateway/templates/authorize.html`
- Create: `src/garmin_gateway/templates/mfa.html`
- Modify: `src/garmin_gateway/oauth.py` (add authorize handlers + in-memory pending-MFA store)
- Test: `tests/test_oauth.py` (extend)

**Interfaces:**
- Produces:
  - `class AuthState: __init__(self, csrf: CsrfStore)` holding: `csrf` and `_mfa: dict[str, tuple]` (login_id → `(pending, oauth_params, ts)`), with `put_mfa(...) -> str` and `pop_mfa(login_id) -> tuple | None` (TTL 300 s, uses `time.monotonic`).
  - `render_authorize(templates, params: dict, csrf_token: str, error: str = "") -> HTMLResponse`
  - `async authorize_get(request, templates, state, conn) -> Response` — validates `client_id`, `redirect_uri` (allowlist), `code_challenge_method == "S256"`; renders the form with a fresh CSRF token and the OAuth params in hidden fields.
  - `async authorize_post(request, templates, state, conn, config) -> Response` — two entry shapes:
    - login step (`garmin_email`,`garmin_password`,`csrf`,oauth params): runs `garmin_login.start_login`; on `ok` → `_finish(...)`; on `needs_mfa` → store pending, render `mfa.html` with a `login_id`; the password local is overwritten/deleted immediately.
    - mfa step (`login_id`,`mfa_code`,`csrf`): `garmin_login.resume_login` → `_finish(...)`.
  - `_finish(conn, config, oauth_params, tokens_json) -> RedirectResponse` — `verify_tokens` to get the account email key (normalized = lowercased submitted email), `upsert_account`, mint auth code (`security.new_secret`, store `hash_token(code)` via `store.create_code`), redirect to `redirect_uri?code=...&state=...` (302).
- Consumes: `garmin_login`, `store`, `security`, `config`.

- [ ] **Step 1: Write `src/garmin_gateway/templates/authorize.html`**

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>Connect Garmin</title>
<style>body{font-family:system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem}
input{display:block;width:100%;padding:.6rem;margin:.4rem 0 1rem;box-sizing:border-box}
button{padding:.6rem 1rem}.err{color:#b00}</style></head>
<body>
<h1>Connect your Garmin account</h1>
<p>Sign in with your Garmin Connect credentials. Your password is used once to
authenticate and is never stored.</p>
{ERROR}
<form method="post" action="/oauth/authorize">
  <input type="hidden" name="csrf" value="{CSRF}">
  <input type="hidden" name="client_id" value="{CLIENT_ID}">
  <input type="hidden" name="redirect_uri" value="{REDIRECT_URI}">
  <input type="hidden" name="state" value="{STATE}">
  <input type="hidden" name="code_challenge" value="{CODE_CHALLENGE}">
  <input type="hidden" name="code_challenge_method" value="{METHOD}">
  <label>Garmin email<input name="garmin_email" type="email" required></label>
  <label>Garmin password<input name="garmin_password" type="password" required></label>
  <button type="submit">Sign in</button>
</form>
</body></html>
```

- [ ] **Step 2: Write `src/garmin_gateway/templates/mfa.html`**

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>Garmin MFA</title>
<style>body{font-family:system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem}
input{display:block;width:100%;padding:.6rem;margin:.4rem 0 1rem;box-sizing:border-box}
button{padding:.6rem 1rem}.err{color:#b00}</style></head>
<body>
<h1>Enter your MFA code</h1>
<p>Garmin sent a verification code to your email or phone.</p>
{ERROR}
<form method="post" action="/oauth/authorize">
  <input type="hidden" name="csrf" value="{CSRF}">
  <input type="hidden" name="login_id" value="{LOGIN_ID}">
  <label>MFA code<input name="mfa_code" inputmode="numeric" required></label>
  <button type="submit">Verify</button>
</form>
</body></html>
```

- [ ] **Step 3: Write the failing test** (extend `tests/test_oauth.py`)

```python
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from starlette.routing import Route
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.templating import Jinja2Templates  # not used; placeholder removed below
from garmin_gateway import oauth, security, garmin_login, store


def _authz_app(conn):
    state = oauth.AuthState(security.CsrfStore())
    async def aget(request):
        return await oauth.authorize_get(request, None, state, conn)
    async def apost(request):
        return await oauth.authorize_post(request, None, state, conn, CONFIG)
    app = Starlette(routes=[
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/authorize", apost, methods=["POST"]),
    ])
    return TestClient(app, follow_redirects=False), state


def _register(conn):
    cid = security.new_secret(8)
    store.create_client(conn, cid, "h", ["https://claude.ai/cb"], "Claude")
    return cid


def test_authorize_get_renders_form(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
        "response_type": "code",
    })
    assert r.status_code == 200
    assert "garmin_email" in r.text
    assert "csrf" in r.text


def test_authorize_get_rejects_bad_redirect(conn):
    client, _ = _authz_app(conn)
    cid = _register(conn)
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": "https://evil.com/cb",
        "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
    })
    assert r.status_code == 400


def test_login_no_mfa_redirects_with_code(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    # obtain a CSRF token the way the GET would mint one
    csrf = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="ok", tokens_json='{"t":1}')), \
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"):
        r = client.post("/oauth/authorize", data={
            "csrf": csrf, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "Me@X.cz", "garmin_password": "pw",
        })
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    assert q["code"]
    # account stored under normalized (lowercased) email
    assert store.get_account_tokens(conn, "me@x.cz", CONFIG.gateway_secret) == '{"t":1}'


def test_login_mfa_then_verify_redirects(conn):
    client, state = _authz_app(conn)
    cid = _register(conn)
    csrf1 = state.csrf.issue()
    with patch.object(garmin_login, "start_login",
                      return_value=garmin_login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        r1 = client.post("/oauth/authorize", data={
            "csrf": csrf1, "client_id": cid, "redirect_uri": "https://claude.ai/cb",
            "state": "xyz", "code_challenge": "abc", "code_challenge_method": "S256",
            "garmin_email": "me@x.cz", "garmin_password": "pw",
        })
    assert r1.status_code == 200 and "login_id" in r1.text
    # extract login_id and a fresh csrf rendered into the MFA page
    import re
    login_id = re.search(r'name="login_id" value="([^"]+)"', r1.text).group(1)
    csrf2 = re.search(r'name="csrf" value="([^"]+)"', r1.text).group(1)
    with patch.object(garmin_login, "resume_login", return_value='{"t":9}'), \
         patch.object(garmin_login, "verify_tokens", return_value="Vaclav S"):
        r2 = client.post("/oauth/authorize", data={
            "csrf": csrf2, "login_id": login_id, "mfa_code": "123456",
        })
    assert r2.status_code == 302
    assert store.get_account_tokens(conn, "me@x.cz", CONFIG.gateway_secret) == '{"t":9}'
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: FAIL (missing `oauth.AuthState`, `authorize_get`, `authorize_post`).

- [ ] **Step 5: Implement authorize handlers in `oauth.py`** (append; add imports at top of file)

```python
# add to the imports already at the top of oauth.py:
import time
from pathlib import Path
from urllib.parse import urlencode
from starlette.responses import HTMLResponse, RedirectResponse
from . import garmin_login

_TPL_DIR = Path(__file__).parent / "templates"


def _tpl(name: str) -> str:
    return (_TPL_DIR / name).read_text()


class AuthState:
    def __init__(self, csrf):
        self.csrf = csrf
        self._mfa: dict[str, tuple] = {}   # login_id -> (pending, oauth_params, ts)

    def put_mfa(self, pending, oauth_params: dict) -> str:
        self._gc()
        from .security import new_secret
        lid = new_secret(18)
        self._mfa[lid] = (pending, oauth_params, time.monotonic())
        return lid

    def pop_mfa(self, login_id: str):
        self._gc()
        item = self._mfa.pop(login_id, None)
        if item is None:
            return None
        pending, params, _ts = item
        return pending, params

    def _gc(self) -> None:
        now = time.monotonic()
        for k, (_p, _q, ts) in list(self._mfa.items()):
            if now - ts > 300:
                self._mfa.pop(k, None)


def _fill(template: str, mapping: dict, error: str = "") -> str:
    out = template.replace("{ERROR}", f'<p class="err">{error}</p>' if error else "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", v)
    return out


def render_authorize(params: dict, csrf_token: str, error: str = "") -> HTMLResponse:
    html = _fill(_tpl("authorize.html"), {
        "CSRF": csrf_token,
        "CLIENT_ID": params.get("client_id", ""),
        "REDIRECT_URI": params.get("redirect_uri", ""),
        "STATE": params.get("state", ""),
        "CODE_CHALLENGE": params.get("code_challenge", ""),
        "METHOD": params.get("code_challenge_method", ""),
    }, error)
    return HTMLResponse(html)


def _oauth_params_from(source) -> dict:
    return {
        "client_id": source.get("client_id", ""),
        "redirect_uri": source.get("redirect_uri", ""),
        "state": source.get("state", ""),
        "code_challenge": source.get("code_challenge", ""),
        "code_challenge_method": source.get("code_challenge_method", ""),
    }


async def authorize_get(request, _templates, state, conn) -> HTMLResponse:
    params = _oauth_params_from(request.query_params)
    client = store.get_client(conn, params["client_id"])
    if client is None:
        return HTMLResponse("unknown client_id", status_code=400)
    if not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid redirect_uri", status_code=400)
    if params["code_challenge_method"] != "S256" or not params["code_challenge"]:
        return HTMLResponse("PKCE S256 required", status_code=400)
    return render_authorize(params, state.csrf.issue())


def _finish(conn, config, params: dict, tokens_json: str, email: str) -> RedirectResponse:
    name = garmin_login.verify_tokens(tokens_json)  # raises on failure
    key = email.strip().lower()
    store.upsert_account(conn, key, tokens_json, config.gateway_secret)
    code = security.new_secret(32)
    store.create_code(
        conn, store.hash_token(code), params["client_id"], params["redirect_uri"],
        params["code_challenge"], params["code_challenge_method"], key,
    )
    sep = "&" if "?" in params["redirect_uri"] else "?"
    location = params["redirect_uri"] + sep + urlencode({"code": code, "state": params["state"]})
    return RedirectResponse(location, status_code=302)


async def authorize_post(request, _templates, state, conn, config) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    if not state.csrf.consume(form.get("csrf", "")):
        return HTMLResponse("invalid or expired CSRF token", status_code=400)

    # MFA step
    if form.get("login_id"):
        popped = state.pop_mfa(form["login_id"])
        if popped is None:
            return HTMLResponse("MFA session expired, please start over", status_code=400)
        pending, params = popped
        try:
            tokens = garmin_login.resume_login(pending, form.get("mfa_code", ""))
            return _finish(conn, config, params, tokens, params["_email"])
        except Exception:  # noqa: BLE001
            lid = state.put_mfa(pending, params)
            html = _fill(_tpl("mfa.html"),
                         {"CSRF": state.csrf.issue(), "LOGIN_ID": lid},
                         "Incorrect or expired code, try again")
            return HTMLResponse(html, status_code=400)

    # login step
    params = _oauth_params_from(form)
    client = store.get_client(conn, params["client_id"])
    if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid client/redirect_uri", status_code=400)
    email = form.get("garmin_email", "")
    password = form.get("garmin_password", "")
    try:
        result = garmin_login.start_login(email, password)
    except Exception:  # noqa: BLE001
        del password
        return render_authorize(params, state.csrf.issue(), "Garmin sign-in failed, check your credentials")
    del password  # discard immediately
    if result.status == "needs_mfa":
        params = {**params, "_email": email}
        lid = state.put_mfa(result.pending, params)
        html = _fill(_tpl("mfa.html"), {"CSRF": state.csrf.issue(), "LOGIN_ID": lid}, "")
        return HTMLResponse(html)
    try:
        return _finish(conn, config, params, result.tokens_json, email)
    except garmin_login.GarminLoginError:
        return render_authorize(params, state.csrf.issue(), "Garmin sign-in could not be verified")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: PASS (8 passed total in the file).

- [ ] **Step 7: Commit**

```bash
git add src/garmin_gateway/oauth.py src/garmin_gateway/templates/authorize.html src/garmin_gateway/templates/mfa.html tests/test_oauth.py
git commit -m "feat: OAuth authorize flow with Garmin login + MFA step"
```

---

### Task 7: OAuth token exchange

**Files:**
- Modify: `src/garmin_gateway/oauth.py` (add `token_exchange`)
- Test: `tests/test_oauth.py` (extend)

**Interfaces:**
- Produces: `async token_exchange(request, conn) -> JSONResponse` — `grant_type=authorization_code`. Validates client (`client_id` + `client_secret` via `hash_token` compare), consumes the code, enforces `redirect_uri` match and `verify_pkce(code_verifier, stored_challenge, stored_method)`. On success mints a Bearer token, stores `hash_token(token) → garmin_user_key`, returns `{access_token, token_type: "Bearer"}`.

- [ ] **Step 1: Write the failing test** (extend `tests/test_oauth.py`)

```python
import hashlib, base64
from starlette.routing import Route
from starlette.applications import Starlette
from starlette.testclient import TestClient


def _token_app(conn):
    async def tok(request):
        return await oauth.token_exchange(request, conn)
    return TestClient(Starlette(routes=[Route("/oauth/token", tok, methods=["POST"])]))


def _pkce_pair():
    verifier = "verifier-abcdef-1234567890"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def test_token_exchange_happy_path(conn):
    cid = security.new_secret(8)
    csecret = "topsecret"
    store.create_client(conn, cid, store.hash_token(csecret), ["https://claude.ai/cb"], "Claude")
    store.upsert_account(conn, "me@x.cz", "{}", CONFIG.gateway_secret)
    verifier, challenge = _pkce_pair()
    code = "thecode"
    store.create_code(conn, store.hash_token(code), cid, "https://claude.ai/cb", challenge, "S256", "me@x.cz")
    c = _token_app(conn)
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/cb",
        "client_id": cid, "client_secret": csecret, "code_verifier": verifier,
    })
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert store.account_key_for_token_hash(conn, store.hash_token(token)) == "me@x.cz"


def test_token_exchange_bad_pkce(conn):
    cid = security.new_secret(8)
    store.create_client(conn, cid, store.hash_token("s"), ["https://claude.ai/cb"], None)
    _, challenge = _pkce_pair()
    store.create_code(conn, store.hash_token("c2"), cid, "https://claude.ai/cb", challenge, "S256", "me@x.cz")
    c = _token_app(conn)
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": "c2", "redirect_uri": "https://claude.ai/cb",
        "client_id": cid, "client_secret": "s", "code_verifier": "WRONG",
    })
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_oauth.py -k token -v`
Expected: FAIL (`oauth.token_exchange` missing).

- [ ] **Step 3: Implement `token_exchange` in `oauth.py`**

```python
async def token_exchange(request, conn) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    client = store.get_client(conn, form.get("client_id", ""))
    if client is None or store.hash_token(form.get("client_secret", "")) != client["client_secret_hash"]:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    row = store.consume_code(conn, store.hash_token(form.get("code", "")))
    if row is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if row["client_id"] != form.get("client_id") or row["redirect_uri"] != form.get("redirect_uri"):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not security.verify_pkce(form.get("code_verifier", ""), row["code_challenge"], row["code_challenge_method"]):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    token = security.new_secret(32)
    store.create_access_token(conn, store.hash_token(token), row["garmin_user_key"], form.get("client_id"))
    return JSONResponse({"access_token": token, "token_type": "Bearer"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: PASS (all oauth tests).

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/oauth.py tests/test_oauth.py
git commit -m "feat: OAuth token exchange with PKCE verification"
```

---

### Task 8: Worker manager

**Files:**
- Create: `src/garmin_gateway/workers.py`
- Test: `tests/test_workers.py`
- Create/Modify: `tests/conftest.py` (add the `fake_worker` fixture used here and in Task 9)

**Interfaces:**
- Produces:
  - `@dataclass WorkerHandle: key: str; port: int; process; last_active: float`
  - `class WorkerManager:`
    - `__init__(self, config, spawn=None, clock=time.monotonic)` — `spawn(key, port, token_dir) -> process` is injectable; default uses `subprocess.Popen(config.garmin_mcp_cmd, env=...)`. A `process` must expose `.poll()` and `.terminate()`.
    - `async ensure_worker(self, key: str, tokens_json: str) -> int` — returns the port; per-key `asyncio.Lock`; reuses a live worker (bumps `last_active`), else materializes tokens + spawns + waits for `/healthz`. Raises `WorkerStartError` if it never becomes healthy.
    - `async reap_idle(self) -> None` — terminate workers idle > `config.worker_idle_ttl`.
    - `def shutdown(self) -> None` — terminate all.
    - `def _materialize_tokens(self, key, tokens_json) -> str` — writes `<data_dir>/users/<safe(key)>/tokens/garmin_tokens.json` (dir `0700`, file `0600`), returns the tokens dir.
  - `class WorkerStartError(Exception)`
- Consumes: `config`, `httpx` (healthz poll), `log`.

- [ ] **Step 1: Add the `fake_worker` fixture to `tests/conftest.py`**

```python
import socket
import threading
import time
import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeWorker:
    """A minimal HTTP server mimicking garmin-mcp's /healthz and /mcp."""
    def __init__(self):
        self.port = _free_port()
        self.calls = []
        worker = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass
            def do_GET(self):
                if self.path == "/healthz":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
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

        self._httpd = HTTPServer(("127.0.0.1", self.port), H)
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._httpd.shutdown()


@pytest.fixture
def fake_worker():
    w = FakeWorker().start()
    # wait until accepting connections
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", w.port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.02)
    yield w
    w.stop()
```

- [ ] **Step 2: Write the failing test** `tests/test_workers.py`

```python
import time
import pytest
from garmin_gateway import workers
from garmin_gateway.config import load_config


def _config(tmp_path, **over):
    env = {"GATEWAY_SECRET": "s" * 40, "DATA_DIR": str(tmp_path), "PUBLIC_URL": "https://x"}
    env.update({k.upper(): str(v) for k, v in over.items()})
    return load_config(env)


async def test_ensure_spawns_and_reuses(tmp_path, fake_worker):
    spawned = []

    class FakeProc:
        def __init__(self): self._alive = True
        def poll(self): return None if self._alive else 0
        def terminate(self): self._alive = False

    def spawn(key, port, token_dir):
        spawned.append((key, port, token_dir))
        return FakeProc()

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, spawn=spawn)
    port1 = await mgr.ensure_worker("me@x.cz", '{"t":1}')
    assert port1 == fake_worker.port
    port2 = await mgr.ensure_worker("me@x.cz", '{"t":1}')
    assert port2 == fake_worker.port
    assert len(spawned) == 1                      # reused, not respawned
    # tokens were materialized
    assert (tmp_path / "users").exists()
    mgr.shutdown()


async def test_ensure_raises_when_never_healthy(tmp_path):
    class DeadProc:
        def poll(self): return 1                  # already exited
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=1, worker_port_start=59999, worker_port_end=59999)
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: DeadProc())
    with pytest.raises(workers.WorkerStartError):
        await mgr.ensure_worker("me@x.cz", "{}")


async def test_reap_idle_terminates(tmp_path, fake_worker):
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: proc, clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", "{}")
    clock[0] = 1100.0                              # advance past idle ttl
    await mgr.reap_idle()
    assert proc.alive is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_workers.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Implement `src/garmin_gateway/workers.py`**

```python
from __future__ import annotations
import asyncio
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
import httpx
from .log import log, log_warn

_SAFE = re.compile(r"[^A-Za-z0-9_.@-]")


class WorkerStartError(Exception):
    pass


@dataclass
class WorkerHandle:
    key: str
    port: int
    process: object
    last_active: float


class WorkerManager:
    def __init__(self, config, spawn=None, clock=time.monotonic):
        self._cfg = config
        self._clock = clock
        self._spawn_fn = spawn or self._default_spawn
        self._workers: dict[str, WorkerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --- public ---------------------------------------------------------

    async def ensure_worker(self, key: str, tokens_json: str) -> int:
        async with self._locks[key]:
            h = self._workers.get(key)
            if h is not None and h.process.poll() is None and await self._healthy(h.port):
                h.last_active = self._clock()
                return h.port
            if h is not None:
                self._terminate(h)
            self._enforce_cap()
            token_dir = self._materialize_tokens(key, tokens_json)
            port = self._alloc_port()
            proc = self._spawn_fn(key, port, token_dir)
            if not await self._wait_healthy(port, proc):
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                raise WorkerStartError(f"worker for {key[:3]}*** failed to become healthy")
            self._workers[key] = WorkerHandle(key, port, proc, self._clock())
            log("worker-started", port=port)
            return port

    async def reap_idle(self) -> None:
        now = self._clock()
        for key, h in list(self._workers.items()):
            if now - h.last_active > self._cfg.worker_idle_ttl or h.process.poll() is not None:
                self._terminate(h)
                self._workers.pop(key, None)
                log("worker-reaped", port=h.port)

    def shutdown(self) -> None:
        for h in list(self._workers.values()):
            self._terminate(h)
        self._workers.clear()

    # --- internals ------------------------------------------------------

    def _enforce_cap(self) -> None:
        while len(self._workers) >= self._cfg.max_workers:
            oldest = min(self._workers.values(), key=lambda h: h.last_active)
            self._terminate(oldest)
            self._workers.pop(oldest.key, None)
            log("worker-evicted", port=oldest.port)

    def _materialize_tokens(self, key: str, tokens_json: str) -> str:
        safe = _SAFE.sub("_", key)
        token_dir = os.path.join(self._cfg.data_dir, "users", safe, "tokens")
        os.makedirs(token_dir, exist_ok=True)
        os.chmod(os.path.join(self._cfg.data_dir, "users", safe), 0o700)
        path = os.path.join(token_dir, "garmin_tokens.json")
        with open(path, "w") as f:
            f.write(tokens_json)
        os.chmod(path, 0o600)
        return token_dir

    def _alloc_port(self) -> int:
        used = {h.port for h in self._workers.values()}
        for p in range(self._cfg.worker_port_start, self._cfg.worker_port_end + 1):
            if p not in used:
                return p
        raise WorkerStartError("no free worker port")

    def _default_spawn(self, key: str, port: int, token_dir: str):
        env = dict(os.environ)
        env.update({
            "GARMIN_MCP_TRANSPORT": "streamable-http",
            "GARMIN_MCP_HOST": "127.0.0.1",
            "GARMIN_MCP_PORT": str(port),
            "GARMINTOKENS": token_dir,
        })
        return subprocess.Popen(self._cfg.garmin_mcp_cmd, env=env)

    def _terminate(self, h: WorkerHandle) -> None:
        try:
            if h.process.poll() is None:
                h.process.terminate()
        except Exception:  # noqa: BLE001
            pass

    async def _healthy(self, port: int) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"http://127.0.0.1:{port}/healthz")
                return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _wait_healthy(self, port: int, proc) -> bool:
        deadline = self._clock() + self._cfg.worker_startup_timeout
        while self._clock() < deadline:
            if proc.poll() is not None:
                return False
            if await self._healthy(port):
                return True
            await asyncio.sleep(0.25)
        return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_workers.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/garmin_gateway/workers.py tests/test_workers.py tests/conftest.py
git commit -m "feat: per-user worker manager (spawn, healthz, idle reaper, cap)"
```

---

### Task 9: MCP reverse proxy

**Files:**
- Create: `src/garmin_gateway/proxy.py`
- Test: `tests/test_proxy.py`

**Interfaces:**
- Produces:
  - `async authenticate(request, conn, rate) -> str | Response` — returns the `garmin_user_key` or an error `Response` (401/429). Bearer parsed from `Authorization: Bearer <t>`.
  - `async handle_mcp(request, method, conn, manager, config, secret, rate) -> Response` — authenticates, loads tokens (`store.get_account_tokens`), `manager.ensure_worker(...)`, forwards to `http://127.0.0.1:<port>/mcp` preserving method/body/`Accept`/`Mcp-Session-Id`, streaming `text/event-stream`. Maps timeout → 504, oversize body → 413, worker start failure → JSON error advising reconnect.
- Consumes: `store`, `security`, `workers`, `httpx`.

- [ ] **Step 1: Write the failing test** `tests/test_proxy.py`

```python
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, proxy, workers, security
from garmin_gateway.config import load_config


def _cfg(tmp_path, fw):
    return load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
                        "DATA_DIR": str(tmp_path),
                        "WORKER_PORT_START": str(fw.port), "WORKER_PORT_END": str(fw.port)})


def _app(conn, mgr, cfg):
    rate = security.RateLimiter()
    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", conn, mgr, cfg, cfg.gateway_secret, rate)
    return TestClient(Starlette(routes=[Route("/mcp", mcp_post, methods=["POST"])]))


class FakeProc:
    def poll(self): return None
    def terminate(self): pass


def test_unauthorized_without_bearer(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0"})
    assert r.status_code == 401


def test_authorized_forwards_to_worker(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-123"
    store.upsert_account(conn, "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id") == "sess-1"
    assert fake_worker.calls and fake_worker.calls[-1][1] == "/mcp"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_proxy.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/garmin_gateway/proxy.py`**

```python
from __future__ import annotations
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from . import store, security
from .workers import WorkerStartError
from .log import log_warn, log_error


async def authenticate(request, conn, rate) -> "str | Response":
    ip = request.client.host if request.client else "unknown"
    if not rate.check(f"unauth:{ip}", limit=30, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token_hash = store.hash_token(header[7:])
    if not rate.check(f"tok:{token_hash}", limit=60, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    key = store.account_key_for_token_hash(conn, token_hash)
    if key is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    return key


async def handle_mcp(request, method, conn, manager, config, secret, rate) -> Response:
    auth = await authenticate(request, conn, rate)
    if isinstance(auth, Response):
        return auth
    key = auth

    body = await security.read_body_limited(request)
    if body is None:
        return JSONResponse({"error": "request_too_large"}, status_code=413)

    tokens = store.get_account_tokens(conn, key, secret)
    if tokens is None:
        return JSONResponse({"error": "unknown_account"}, status_code=401)

    try:
        port = await manager.ensure_worker(key, tokens)
    except WorkerStartError:
        return JSONResponse(
            {"error": "garmin_session_expired",
             "message": "Your Garmin session expired. Please reconnect the Garmin MCP server."},
            status_code=502,
        )

    upstream_headers = {}
    accept = request.headers.get("accept")
    if accept:
        upstream_headers["Accept"] = accept
    sid = request.headers.get("mcp-session-id")
    if sid:
        if not security.validate_session_id(sid):
            return JSONResponse({"error": "invalid_session_id"}, status_code=400)
        upstream_headers["Mcp-Session-Id"] = sid
    if method != "DELETE":
        upstream_headers["Content-Type"] = "application/json"

    url = f"http://127.0.0.1:{port}/mcp"
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    try:
        req = client.build_request(method, url, headers=upstream_headers,
                                   content=body if method != "GET" else None)
        upstream = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return JSONResponse({"error": "gateway_timeout"}, status_code=504)
    except httpx.HTTPError as e:
        await client.aclose()
        log_error("mcp-forward-error", error=type(e).__name__)
        return JSONResponse({"error": "bad_gateway"}, status_code=502)

    resp_headers = {}
    ct = upstream.headers.get("content-type")
    if ct:
        resp_headers["Content-Type"] = ct
    up_sid = upstream.headers.get("mcp-session-id")
    if up_sid:
        resp_headers["Mcp-Session-Id"] = up_sid

    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=resp_headers)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_proxy.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/proxy.py tests/test_proxy.py
git commit -m "feat: MCP reverse proxy to per-user worker (SSE, session, limits)"
```

---

### Task 10: Starlette app wiring + lifespan + landing page

**Files:**
- Create: `src/garmin_gateway/app.py`
- Create: `src/garmin_gateway/templates/landing.html`
- Test: `tests/test_app.py`

**Interfaces:**
- Produces:
  - `build_app(config) -> Starlette` — wires routes, middleware (security headers via a `BaseHTTPMiddleware` or response post-processing), shared singletons (`conn`, `WorkerManager`, `AuthState`, `RateLimiter`), and a lifespan that starts a periodic `reap_idle` + `cleanup_expired_codes` task and tears down the manager on shutdown.
  - `main()` — console-script entry: `load_config()` then `uvicorn.run(build_app(cfg), host="0.0.0.0", port=cfg.port)`.
- Routes: `GET /`, `GET /healthz`, `GET /.well-known/oauth-authorization-server`, `POST /oauth/register`, `GET|POST /oauth/authorize`, `POST /oauth/token`, `POST|GET|DELETE /mcp`.

- [ ] **Step 1: Write `src/garmin_gateway/templates/landing.html`**

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>Garmin MCP Gateway</title>
<style>body{font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem;line-height:1.5}
code{background:#f2f2f2;padding:.1rem .3rem;border-radius:.2rem}</style></head>
<body>
<h1>Garmin MCP Gateway</h1>
<p>Add this server as a remote MCP server in Claude using the URL
<code>{PUBLIC_URL}/mcp</code>. You will sign in with your Garmin Connect
account; your password is used once and never stored.</p>
<p>Operated by {OPERATOR_NAME}{OPERATOR_EMAIL}.</p>
</body></html>
```

- [ ] **Step 2: Write the failing test** `tests/test_app.py`

```python
from starlette.testclient import TestClient
from garmin_gateway.app import build_app
from garmin_gateway.config import load_config


def _client(tmp_path):
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "t.db")})
    return TestClient(build_app(cfg))


def test_landing_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "/mcp" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_healthz(tmp_path):
    c = _client(tmp_path)
    assert c.get("/healthz").text == "ok"


def test_metadata_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-authorization-server").json()
    assert m["issuer"] == "https://gw.example.com"


def test_mcp_requires_auth(tmp_path):
    c = _client(tmp_path)
    assert c.post("/mcp", json={}).status_code == 401
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL (`ModuleNotFoundError: garmin_gateway.app` has no `build_app`).

- [ ] **Step 4: Implement `src/garmin_gateway/app.py`**

```python
from __future__ import annotations
import asyncio
import contextlib
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware
from . import store, oauth, proxy, security
from .config import load_config, Config
from .workers import WorkerManager
from .log import log

_TPL = Path(__file__).parent / "templates"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        for k, v in security.security_headers().items():
            resp.headers.setdefault(k, v)
        return resp


def build_app(config: Config) -> Starlette:
    conn = store.init_db(config.db_path)
    manager = WorkerManager(config)
    auth_state = oauth.AuthState(security.CsrfStore())
    rate = security.RateLimiter()

    landing = (_TPL / "landing.html").read_text().replace(
        "{PUBLIC_URL}", config.public_url
    ).replace("{OPERATOR_NAME}", config.operator_name).replace(
        "{OPERATOR_EMAIL}", f" ({config.operator_email})" if config.operator_email else ""
    )

    async def home(request):
        return HTMLResponse(landing)

    async def healthz(request):
        return PlainTextResponse("ok")

    async def meta(request):
        return JSONResponse(oauth.metadata(config))

    async def register(request):
        if not rate.check(f"oauth:{request.client.host}", 20, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await oauth.register_client(request, conn)

    async def authz_get(request):
        return await oauth.authorize_get(request, None, auth_state, conn)

    async def authz_post(request):
        if not rate.check(f"login:{request.client.host}", 5, 60):
            return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
        return await oauth.authorize_post(request, None, auth_state, conn, config)

    async def token(request):
        if not rate.check(f"oauth:{request.client.host}", 20, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await oauth.token_exchange(request, conn)

    def mcp(method):
        async def handler(request):
            return await proxy.handle_mcp(request, method, conn, manager, config, config.gateway_secret, rate)
        return handler

    @contextlib.asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def loop():
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    await manager.reap_idle()
                    store.cleanup_expired_codes(conn)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=60)

        task = asyncio.create_task(loop())
        log("gateway-started", port=config.port)
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            manager.shutdown()

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", meta, methods=["GET"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authz_get, methods=["GET"]),
        Route("/oauth/authorize", authz_post, methods=["POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/mcp", mcp("POST"), methods=["POST"]),
        Route("/mcp", mcp("GET"), methods=["GET"]),
        Route("/mcp", mcp("DELETE"), methods=["DELETE"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def main() -> None:
    import uvicorn
    config = load_config()
    uvicorn.run(build_app(config), host="0.0.0.0", port=config.port)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS (all tasks' tests green).

- [ ] **Step 7: Commit**

```bash
git add src/garmin_gateway/app.py src/garmin_gateway/templates/landing.html tests/test_app.py
git commit -m "feat: Starlette app wiring, lifespan reaper, landing page"
```

---

### Task 11: Deployment — Docker, Compose, nginx, env, README

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.env.example`, `nginx.conf.example`, `README.md`

**Interfaces:** none (deployment artifacts). Validation is a successful image build and a local smoke run.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app

# Pin the unmodified garmin_mcp worker by commit (override at build time).
ARG GARMIN_MCP_REF=main
ENV GARMIN_MCP_REF=${GARMIN_MCP_REF}

COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --system . && \
    uv pip install --system "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp@${GARMIN_MCP_REF}"

# tini reaps the many worker subprocesses the gateway spawns
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["tini", "--"]
CMD ["garmin-gateway"]
EXPOSE 8080
VOLUME ["/data"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  gateway:
    build:
      context: .
      args:
        GARMIN_MCP_REF: ${GARMIN_MCP_REF:-main}
    init: true                       # reap worker subprocesses
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:8080:8080"        # nginx (host) proxies to this
    volumes:
      - gateway-data:/data

volumes:
  gateway-data:
```

- [ ] **Step 3: Write `.env.example`**

```bash
# Required: encryption key for stored Garmin tokens (>=32 chars)
GATEWAY_SECRET=change-me-to-a-long-random-string-min-32-chars
# Public HTTPS URL nginx serves (used in OAuth metadata + redirects)
PUBLIC_URL=https://garmin-gw.example.com
# Pin the garmin_mcp worker to a reviewed commit (supply-chain)
GARMIN_MCP_REF=main
# Optional tuning
PORT=8080
DATA_DIR=/data
WORKER_PORT_START=9000
WORKER_PORT_END=9099
WORKER_IDLE_TTL=900
MAX_WORKERS=10
OPERATOR_NAME=Your Name
OPERATOR_EMAIL=you@example.com
```

- [ ] **Step 4: Write `nginx.conf.example`** (same shape as the rohlik setup)

```nginx
server {
    listen 443 ssl http2;
    server_name garmin-gw.example.com;

    ssl_certificate     /etc/letsencrypt/live/garmin-gw.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/garmin-gw.example.com/privkey.pem;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE streaming for /mcp
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

- [ ] **Step 5: Write `README.md`**

```markdown
# Garmin MCP Gateway

A multi-user OAuth 2.1 gateway that lets a small trusted circle connect their
own Garmin account to Claude (mobile/desktop/web), by wrapping the unmodified
[`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) worker.

```
Claude → POST /mcp (Bearer) → Gateway → 127.0.0.1:<port>/mcp (per-user garmin-mcp) → connect.garmin.com
```

## Quick start

```bash
cp .env.example .env          # set GATEWAY_SECRET, PUBLIC_URL, GARMIN_MCP_REF
docker compose up -d --build
```

Put nginx in front for TLS + your domain (see `nginx.conf.example`), then add
`https://<your-domain>/mcp` as a remote MCP server in Claude.

## How it works

1. Claude registers a client (DCR) and starts OAuth 2.1 (Authorization Code + PKCE).
2. On the authorize page the user signs in with Garmin (email + password, + MFA
   if prompted). The gateway logs in via `garminconnect`, stores **only the
   resulting tokens** (encrypted), and discards the password.
3. Claude exchanges the code for a Bearer token.
4. On each `/mcp` call the gateway ensures the user's `garmin-mcp` worker is
   running (its own tokens, bound to `127.0.0.1`) and reverse-proxies to it.

## Security

- Garmin password is never persisted.
- Tokens encrypted at rest (AES-256-GCM); the DB is useless without `GATEWAY_SECRET`.
- Bearer tokens stored only as SHA-256 hashes.
- OAuth 2.1 PKCE (S256), one-time 10-min codes, CSRF on forms, per-IP/-token rate limits.
- Workers bind `127.0.0.1` only; `garmin_mcp` is pinned to a reviewed commit.

> Deploy only on infrastructure you control and trust. Back up `/data`; keep
> `GATEWAY_SECRET` separately.

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest -v
```
```

- [ ] **Step 6: Validate the image builds and the suite passes**

Run: `docker compose build && uv run pytest -v`
Expected: image builds; all tests pass.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example nginx.conf.example README.md
git commit -m "feat: Docker Compose deployment, nginx example, README"
```

---

## Self-Review

**Spec coverage**

| Spec item | Task |
|---|---|
| OAuth 2.1 endpoints (metadata, DCR, authorize, token, /mcp) | 5, 6, 7, 9, 10 |
| Garmin web-login → store tokens only, password discarded | 4, 6 |
| MFA two-step flow | 4, 6 |
| Per-user `garmin-mcp` subprocess, 127.0.0.1, env contract | 8 |
| Lazy spawn, idle eviction, cap/LRU, respawn | 8 |
| Reverse-proxy `/mcp` (SSE, Mcp-Session-Id, timeouts, body limit) | 9 |
| SQLite data model (accounts, access_tokens, clients, codes) | 2 |
| AES-256-GCM, token hashing, file perms | 2 |
| PKCE S256, CSRF, redirect allowlist, rate limits, headers | 3, 6, 7, 9, 10 |
| Persistence of who logged in (survives restart) | 2 (SQLite on /data) + 10 (db_path under DATA_DIR) |
| Docker Compose + nginx + pinned worker | 11 |
| Testing strategy (mocked garminconnect, fake worker) | 4, 8, 9 |

No gaps found.

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Template files use literal `{TOKEN}` placeholders that code fills via `str.replace`; these are intentional, not plan gaps.

**Type consistency:** `garmin_user_key` used uniformly as the normalized lowercased email; `hash_token`/`account_key_for_token_hash`/`create_access_token` signatures match across tasks 2, 6, 7, 9; `ensure_worker(key, tokens_json) -> int` consumed consistently by task 9; `LoginResult(status, tokens_json, pending)` consistent across tasks 4 and 6.

**Known follow-ups (non-blocking):** optional admin endpoint to list/revoke accounts (store already supports `list_accounts`); `_dump_tokens` call sites both pass the garth client (`g.client`) per the Task 4 note — verify against the pinned `garminconnect` version during Task 4.
