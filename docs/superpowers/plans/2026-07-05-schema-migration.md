# Schema Migration to Adapter-Aware Storage — Implementation Plan (Plan A1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the SQLite store from Garmin-specific (`garmin_accounts`, `garmin_user_key`) to the generic, adapter-keyed schema (`accounts(adapter, account_key, blob_enc)` + an `adapter` column on the other tables) — spec Part 1 — so a future adapter needs no schema change. **SQLite stays** (single-node by design; decided 2026-07-05).

**Architecture:** One guarded, idempotent migration (`PRAGMA user_version` 0→1) rewrites an existing DB in place; ciphertext moves verbatim (no re-encryption). `store.py` CRUD becomes `(adapter, account_key)`-keyed; oauth/proxy pass the adapter (already threaded from the adapter-seam work) into every store call; the `token-issued` log field `garmin_user_key=` becomes `account_key=`. Operator scripts are updated to the new column names so they keep working. **No URL/route change** — that is Plan A2 (path routing).

**Tech Stack:** Python 3.12, Starlette, sqlite3 (stdlib), pytest via `uv run --extra dev pytest`.

## Global Constraints

- **Behavior-identical except storage internals + one log field.** Same routes, same HTTP statuses, same HTML, same OAuth flow. The ONLY intended observable change is the `token-issued` structured-log field renamed `garmin_user_key=` → `account_key=`. No `scripts/*` consumes that log field (verified: `scripts/health.py` buckets on event names + `status`/`reason`, not on `garmin_user_key`).
- **Ciphertext is preserved verbatim** by the migration — no decrypt/re-encrypt. Crypto unchanged: key = SHA-256(`GATEWAY_SECRET`), `nonce:ciphertext` hex.
- **Migration is guarded + idempotent:** runs only when `PRAGMA user_version == 0` and a legacy `garmin_accounts` table exists; stamps `user_version = 1`; re-running `init_db` is a no-op.
- Test command: `uv run --extra dev pytest -q` (the `--extra dev` is REQUIRED; plain `uv run pytest` fails). **Baseline before Task 1: 87 passed.** Run the full suite at the end of every task; it must be green before the commit step.
- SQLite specifics that must hold: `ALTER TABLE ... ADD COLUMN <x> NOT NULL DEFAULT 'garmin'` (the DEFAULT is required to satisfy NOT NULL on existing rows and is harmless afterward — the code always supplies `adapter` explicitly); `ALTER TABLE ... RENAME COLUMN` (SQLite ≥3.25, bundled with Python 3.12); `tool_usage` PK changes to `(adapter, account_key, tool)` so it is **rebuilt** (create-new → copy → drop → rename), not altered in place.
- Python 3.12; source under `src/garmin_gateway/`, tests under `tests/`.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (repo commits as `vaclav@slajs.eu`).
- **Out of scope (do NOT do here):** path routing / `/garmin/mcp` / `.well-known` changes (Plan A2); adapter-isolation enforcement beyond what Task 2 specifies; Rohlik; worker-registry composite keying (the worker key stays `account_key` — a second adapter will revisit it); package rename.

## File Structure

```
src/garmin_gateway/
  store.py    — new adapter-aware schema (_SCHEMA), guarded migration (_migrate/_migrate_v1),
                all CRUD re-keyed to (adapter, account_key)
  oauth.py    — register_client/_finish/token_exchange pass adapter; token-issued log field
  proxy.py    — handle_mcp passes adapter.name into get_account_tokens/record_usage
  app.py      — register handler passes the garmin adapter to register_client
scripts/
  status.py, revoke.py, usage.py — column refs updated (accounts / account_key)
tests/
  test_store.py  — all CRUD calls to new signatures + a v0→v1 migration test
  test_oauth.py  — store call sites to new signatures
  test_proxy.py  — store call sites to new signatures
```

---

### Task 1: Adapter-aware schema + guarded migration + CRUD + call sites

The atomic core. The store's schema and its CRUD signatures and their call sites are tightly coupled (return-shape changes break consumers), so they move in one commit. Ends with the whole suite green (87) plus one new migration test (→ 88).

**Files:**
- Modify: `src/garmin_gateway/store.py` (schema, migration, all CRUD)
- Modify: `src/garmin_gateway/oauth.py` (`register_client`, `_finish`, `authorize_post` call sites, `token_exchange`)
- Modify: `src/garmin_gateway/proxy.py` (`handle_mcp` store calls)
- Modify: `src/garmin_gateway/app.py` (`register` handler)
- Modify: `tests/test_store.py`, `tests/test_oauth.py`, `tests/test_proxy.py`

**Interfaces produced (later tasks + Plan A2 rely on these exact signatures):**
- `store.upsert_account(conn, adapter, account_key, blob, secret) -> None`
- `store.get_account_tokens(conn, adapter, account_key, secret) -> str | None`
- `store.list_accounts(conn) -> list[dict]` (rows have `adapter`, `account_key`, `created_at`, `updated_at`)
- `store.create_access_token(conn, token_hash, adapter, account_key, client_id, ttl=0) -> None`
- `store.account_key_for_token_hash(conn, token_hash) -> str | None` (**still returns `account_key`**; Task 2 changes it to a tuple)
- `store.revoke_account(conn, adapter, account_key) -> int`
- `store.revoke_token(conn, token_hash) -> int` (unchanged)
- `store.create_client(conn, client_id, client_secret_hash, redirect_uris, client_name, adapter) -> None`
- `store.get_client(conn, client_id) -> dict | None` (unchanged)
- `store.create_code(conn, code_hash, client_id, redirect_uri, code_challenge, method, adapter, account_key, ttl=600) -> None`
- `store.consume_code(conn, code_hash) -> dict | None` (keys: `adapter`, `account_key`, `client_id`, `redirect_uri`, `code_challenge`, `code_challenge_method`)
- `store.record_usage(conn, adapter, account_key, tool) -> None`
- `store.stats_counts(conn) -> dict` (unchanged keys)
- `oauth.register_client(request, conn, adapter)` — `adapter` is the adapter object; uses `adapter.name`
- `oauth._finish(conn, config, params, blob, adapter_name, account_key)`

- [ ] **Step 1: Write the failing migration test**

Append to `tests/test_store.py`:

```python
def _build_v0_db(path):
    """Create a pre-migration (v0) DB with the OLD Garmin-specific schema and one
    row per table, so we can prove the migration preserves data."""
    import sqlite3
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE garmin_accounts (
            garmin_user_key TEXT PRIMARY KEY, garmin_tokens_enc TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE access_tokens (
            token_hash TEXT PRIMARY KEY, garmin_user_key TEXT NOT NULL, client_id TEXT,
            created_at TEXT DEFAULT (datetime('now')), last_used TEXT, expires_at INTEGER);
        CREATE TABLE oauth_clients (
            client_id TEXT PRIMARY KEY, client_secret_hash TEXT NOT NULL,
            redirect_uris TEXT NOT NULL, client_name TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE oauth_codes (
            code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
            code_challenge TEXT, code_challenge_method TEXT, garmin_user_key TEXT NOT NULL,
            expires_at INTEGER NOT NULL, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE tool_usage (
            garmin_user_key TEXT NOT NULL, tool TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
            last_used TEXT, PRIMARY KEY (garmin_user_key, tool));
        INSERT INTO garmin_accounts (garmin_user_key, garmin_tokens_enc) VALUES ('me@x.cz', 'NONCE:CIPHER');
        INSERT INTO access_tokens (token_hash, garmin_user_key, client_id) VALUES ('th1', 'me@x.cz', 'c1');
        INSERT INTO oauth_clients (client_id, client_secret_hash, redirect_uris, client_name)
            VALUES ('c1', 'sh', '["https://a/cb"]', 'Claude');
        INSERT INTO tool_usage (garmin_user_key, tool, calls) VALUES ('me@x.cz', 'get_activities', 5);
    """)
    c.commit(); c.close()


def test_migration_v0_to_v1_preserves_data(tmp_path):
    db = str(tmp_path / "old.db")
    _build_v0_db(db)
    conn = store.init_db(db)                      # runs the migration
    # accounts carries the old garmin row under adapter='garmin', ciphertext verbatim
    row = conn.execute(
        "SELECT adapter, account_key, blob_enc FROM accounts").fetchone()
    assert (row["adapter"], row["account_key"], row["blob_enc"]) == ("garmin", "me@x.cz", "NONCE:CIPHER")
    # renamed column + backfilled adapter on the other tables
    at = conn.execute("SELECT adapter, account_key FROM access_tokens WHERE token_hash='th1'").fetchone()
    assert (at["adapter"], at["account_key"]) == ("garmin", "me@x.cz")
    tu = conn.execute("SELECT adapter, account_key, calls FROM tool_usage").fetchone()
    assert (tu["adapter"], tu["account_key"], tu["calls"]) == ("garmin", "me@x.cz", 5)
    assert conn.execute("SELECT adapter FROM oauth_clients WHERE client_id='c1'").fetchone()["adapter"] == "garmin"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='garmin_accounts'").fetchone()[0] == 0
    conn.close()


def test_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "old.db")
    _build_v0_db(db)
    store.init_db(db).close()          # migrate once
    conn = store.init_db(db)           # re-open: must be a no-op, data intact
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()
```

- [ ] **Step 2: Run the migration test to verify it fails**

Run: `uv run --extra dev pytest tests/test_store.py::test_migration_v0_to_v1_preserves_data -v`
Expected: FAIL — `init_db` still builds the old schema, so `accounts` doesn't exist (`sqlite3.OperationalError: no such table: accounts`).

- [ ] **Step 3: Rewrite the schema + migration in `store.py`**

Replace the `# --- schema ---` section (the current `init_db`) with:

```python
# --- schema ---------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    adapter     TEXT NOT NULL,
    account_key TEXT NOT NULL,
    blob_enc    TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (adapter, account_key)
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token_hash  TEXT PRIMARY KEY,
    adapter     TEXT NOT NULL,
    account_key TEXT NOT NULL,
    client_id   TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    last_used   TEXT,
    expires_at  INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id          TEXT PRIMARY KEY,
    adapter            TEXT NOT NULL,
    client_secret_hash TEXT NOT NULL,
    redirect_uris      TEXT NOT NULL,
    client_name        TEXT,
    created_at         TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash             TEXT PRIMARY KEY,
    adapter               TEXT NOT NULL,
    client_id             TEXT NOT NULL,
    redirect_uri          TEXT NOT NULL,
    code_challenge        TEXT,
    code_challenge_method TEXT,
    account_key           TEXT NOT NULL,
    expires_at            INTEGER NOT NULL,
    created_at            TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tool_usage (
    adapter     TEXT NOT NULL,
    account_key TEXT NOT NULL,
    tool        TEXT NOT NULL,
    calls       INTEGER NOT NULL DEFAULT 0,
    last_used   TEXT,
    PRIMARY KEY (adapter, account_key, tool)
);
"""

# One-time transform from the pre-adapter (v0) schema. Ciphertext moves verbatim.
# ADD COLUMN needs a DEFAULT to satisfy NOT NULL on existing rows; the default is
# harmless afterward (the code always supplies `adapter`). tool_usage's PK changes,
# so it is rebuilt rather than altered.
_MIGRATE_V1 = [
    """CREATE TABLE accounts (
        adapter TEXT NOT NULL, account_key TEXT NOT NULL, blob_enc TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (adapter, account_key))""",
    """INSERT INTO accounts (adapter, account_key, blob_enc, created_at, updated_at)
        SELECT 'garmin', garmin_user_key, garmin_tokens_enc, created_at, updated_at
        FROM garmin_accounts""",
    "DROP TABLE garmin_accounts",
    "ALTER TABLE access_tokens ADD COLUMN adapter TEXT NOT NULL DEFAULT 'garmin'",
    "ALTER TABLE access_tokens RENAME COLUMN garmin_user_key TO account_key",
    "ALTER TABLE oauth_codes ADD COLUMN adapter TEXT NOT NULL DEFAULT 'garmin'",
    "ALTER TABLE oauth_codes RENAME COLUMN garmin_user_key TO account_key",
    "ALTER TABLE oauth_clients ADD COLUMN adapter TEXT NOT NULL DEFAULT 'garmin'",
    """CREATE TABLE tool_usage_new (
        adapter TEXT NOT NULL, account_key TEXT NOT NULL, tool TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0, last_used TEXT,
        PRIMARY KEY (adapter, account_key, tool))""",
    """INSERT INTO tool_usage_new (adapter, account_key, tool, calls, last_used)
        SELECT 'garmin', garmin_user_key, tool, calls, last_used FROM tool_usage""",
    "DROP TABLE tool_usage",
    "ALTER TABLE tool_usage_new RENAME TO tool_usage",
]


def _migrate(conn) -> None:
    """Bring an existing DB to the current schema version. Guarded by
    PRAGMA user_version; idempotent. Fresh DBs (no legacy table) are just stamped."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    has_legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='garmin_accounts'"
    ).fetchone() is not None
    if has_legacy:
        with conn:                       # transaction: commit on success, rollback on error
            for stmt in _MIGRATE_V1:
                conn.execute(stmt)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)                       # transform an old DB before creating fresh tables
    conn.executescript(_SCHEMA)          # create target tables for a fresh DB; no-op otherwise
    conn.commit()
    # Back-compat: DBs created before access_tokens had expires_at.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(access_tokens)")}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE access_tokens ADD COLUMN expires_at INTEGER")
        conn.commit()
    if db_path not in (":memory:", ""):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(db_path + suffix, 0o600)
            except OSError:
                pass
    return conn
```

- [ ] **Step 4: Run the migration tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_store.py::test_migration_v0_to_v1_preserves_data tests/test_store.py::test_migration_is_idempotent -v`
Expected: 2 passed.

- [ ] **Step 5: Rewrite the CRUD functions in `store.py` to the adapter-aware signatures**

Replace the accounts / access-token / client / code / usage CRUD with:

```python
# --- accounts -------------------------------------------------------------

def upsert_account(conn, adapter: str, account_key: str, blob: str, secret: str) -> None:
    enc = encrypt(secret, blob)
    conn.execute(
        """INSERT INTO accounts (adapter, account_key, blob_enc)
           VALUES (?, ?, ?)
           ON CONFLICT(adapter, account_key)
           DO UPDATE SET blob_enc=excluded.blob_enc, updated_at=datetime('now')""",
        (adapter, account_key, enc),
    )
    conn.commit()


def get_account_tokens(conn, adapter: str, account_key: str, secret: str) -> str | None:
    row = conn.execute(
        "SELECT blob_enc FROM accounts WHERE adapter=? AND account_key=?",
        (adapter, account_key),
    ).fetchone()
    if row is None:
        return None
    return decrypt(secret, row["blob_enc"])


def list_accounts(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT adapter, account_key, created_at, updated_at FROM accounts ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def stats_counts(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "accounts": one("SELECT COUNT(*) FROM accounts"),
        "tokens": one("SELECT COUNT(*) FROM access_tokens"),
        "people_with_token": one(
            "SELECT COUNT(DISTINCT adapter || ':' || account_key) FROM access_tokens"),
        "clients": one("SELECT COUNT(*) FROM oauth_clients"),
        "pending_codes": one("SELECT COUNT(*) FROM oauth_codes"),
    }


# --- access tokens --------------------------------------------------------

def create_access_token(conn, token_hash: str, adapter: str, account_key: str,
                        client_id: str, ttl: int = 0) -> None:
    expires_at = int(time.time()) + ttl if ttl else None
    conn.execute(
        "INSERT OR REPLACE INTO access_tokens "
        "(token_hash, adapter, account_key, client_id, last_used, expires_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), ?)",
        (token_hash, adapter, account_key, client_id, expires_at),
    )
    conn.commit()


def account_key_for_token_hash(conn, token_hash: str) -> str | None:
    # Returns account_key (str). Task 2 (adapter isolation) changes this to
    # return (adapter, account_key) so the proxy can reject cross-adapter tokens.
    row = conn.execute(
        "SELECT account_key, expires_at FROM access_tokens WHERE token_hash=?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and time.time() > row["expires_at"]:
        return None
    conn.execute(
        "UPDATE access_tokens SET last_used=datetime('now') WHERE token_hash=?",
        (token_hash,),
    )
    conn.commit()
    return row["account_key"]


def cleanup_expired_tokens(conn) -> None:
    conn.execute(
        "DELETE FROM access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
        (int(time.time()),),
    )
    conn.commit()


def revoke_token(conn, token_hash: str) -> int:
    cur = conn.execute("DELETE FROM access_tokens WHERE token_hash=?", (token_hash,))
    conn.commit()
    return cur.rowcount


def revoke_account(conn, adapter: str, account_key: str) -> int:
    cur = conn.execute(
        "DELETE FROM access_tokens WHERE adapter=? AND account_key=?",
        (adapter, account_key),
    )
    conn.commit()
    return cur.rowcount


# --- oauth clients --------------------------------------------------------

def create_client(conn, client_id, client_secret_hash, redirect_uris: list[str],
                  client_name, adapter: str) -> None:
    conn.execute(
        "INSERT INTO oauth_clients (client_id, adapter, client_secret_hash, redirect_uris, client_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (client_id, adapter, client_secret_hash, json.dumps(redirect_uris), client_name),
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

def create_code(conn, code_hash, client_id, redirect_uri, code_challenge, method,
                adapter: str, account_key: str, ttl=600) -> None:
    conn.execute(
        "INSERT INTO oauth_codes (code_hash, adapter, client_id, redirect_uri, code_challenge, "
        "code_challenge_method, account_key, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code_hash, adapter, client_id, redirect_uri, code_challenge, method,
         account_key, int(time.time()) + ttl),
    )
    conn.commit()


def consume_code(conn, code_hash) -> dict | None:
    row = conn.execute(
        "SELECT adapter, client_id, redirect_uri, code_challenge, code_challenge_method, "
        "account_key, expires_at FROM oauth_codes WHERE code_hash=?",
        (code_hash,),
    ).fetchone()
    conn.execute("DELETE FROM oauth_codes WHERE code_hash=?", (code_hash,))
    conn.commit()
    if row is None or time.time() > row["expires_at"]:
        return None
    return {
        "adapter": row["adapter"],
        "client_id": row["client_id"],
        "redirect_uri": row["redirect_uri"],
        "code_challenge": row["code_challenge"],
        "code_challenge_method": row["code_challenge_method"],
        "account_key": row["account_key"],
    }


def cleanup_expired_codes(conn) -> None:
    conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (int(time.time()),))
    conn.commit()


# --- usage metrics --------------------------------------------------------

def record_usage(conn, adapter: str, account_key: str, tool: str) -> None:
    conn.execute(
        "INSERT INTO tool_usage (adapter, account_key, tool, calls, last_used) "
        "VALUES (?, ?, ?, 1, datetime('now')) "
        "ON CONFLICT(adapter, account_key, tool) DO UPDATE SET "
        "calls = calls + 1, last_used = datetime('now')",
        (adapter, account_key, tool),
    )
    conn.commit()
```

- [ ] **Step 6: Update the call sites in `oauth.py`**

`register_client` — gains the adapter object (line ~30):
```python
async def register_client(request, conn, adapter) -> JSONResponse:
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
    store.create_client(conn, client_id, store.hash_token(client_secret), uris,
                        data.get("client_name"), adapter.name)
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

`_finish` — gains `adapter_name`, stores it (line ~134):
```python
def _finish(conn, config, params: dict, blob: str, adapter_name: str, account_key: str) -> RedirectResponse:
    # blob already verified by the caller (adapter.verify) before we persist
    store.upsert_account(conn, adapter_name, account_key, blob, config.gateway_secret)
    code = security.new_secret(32)
    store.create_code(
        conn, store.hash_token(code), params["client_id"], params["redirect_uri"],
        params["code_challenge"], params["code_challenge_method"], adapter_name, account_key,
    )
    sep = "&" if "?" in params["redirect_uri"] else "?"
    location = params["redirect_uri"] + sep + urlencode({"code": code, "state": params["state"]})
    return RedirectResponse(location, status_code=302)
```

`authorize_post` — both `_finish` calls pass `adapter.name` (the MFA-path call ~line 184 and the login-path call ~line 215):
```python
        return _finish(conn, config, params, result.blob, adapter.name, result.account_key)
```
(apply to BOTH `_finish(...)` calls in `authorize_post`).

`token_exchange` — read adapter/account_key from the consumed code; rename the log field (lines ~241-244):
```python
    token = security.new_secret(32)
    store.create_access_token(conn, store.hash_token(token), row["adapter"], row["account_key"],
                              form.get("client_id"), ttl=config.access_token_ttl)
    log("token-issued", account_key=row["account_key"])
```

- [ ] **Step 7: Update the call sites in `proxy.py` and `app.py`**

`proxy.py` `handle_mcp` — pass `adapter.name` into the two store calls (lines ~61, ~68):
```python
    tokens = store.get_account_tokens(conn, adapter.name, key, secret)
    if tokens is None:
        return JSONResponse({"error": "unknown_account"}, status_code=401)

    tool = _mcp_tool(body)
    if tool:
        try:
            store.record_usage(conn, adapter.name, key, tool)
        except Exception:  # noqa: BLE001 - usage metrics must never break a request
            pass
```
(`ensure_worker(key, tokens)` stays keyed by `key` — worker-registry composite keying is out of scope.)

`app.py` `register` handler (line ~65) — pass the garmin adapter:
```python
    async def register(request):
        if not rate.check(f"oauth:{request.client.host}", 20, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await oauth.register_client(request, conn, garmin)
```

- [ ] **Step 8: Update the existing store tests to the new signatures** (`tests/test_store.py`)

Rewrite every CRUD call. The crypto/hash tests (`test_crypto_roundtrip`, `test_decrypt_with_wrong_secret_fails`, `test_hash_token_is_stable_and_hex`) are unchanged. The rest:

```python
def test_account_upsert_and_fetch(conn):
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', SECRET)
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", SECRET) == '{"t":1}'
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":2}', SECRET)
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", SECRET) == '{"t":2}'
    assert store.get_account_tokens(conn, "garmin", "absent@x.cz", SECRET) is None


def test_access_token_maps_to_account(conn):
    store.upsert_account(conn, "garmin", "me@x.cz", "{}", SECRET)
    store.create_access_token(conn, "hash1", "garmin", "me@x.cz", "client1")
    assert store.account_key_for_token_hash(conn, "hash1") == "me@x.cz"
    assert store.account_key_for_token_hash(conn, "nope") is None


def test_access_token_expiry(conn):
    store.create_access_token(conn, "h_exp", "garmin", "me@x.cz", "c1", ttl=-1)
    store.create_access_token(conn, "h_live", "garmin", "me@x.cz", "c1", ttl=3600)
    store.create_access_token(conn, "h_forever", "garmin", "me@x.cz", "c1")
    assert store.account_key_for_token_hash(conn, "h_exp") is None
    assert store.account_key_for_token_hash(conn, "h_live") == "me@x.cz"
    assert store.account_key_for_token_hash(conn, "h_forever") == "me@x.cz"


def test_cleanup_expired_tokens(conn):
    store.create_access_token(conn, "h_exp", "garmin", "me@x.cz", "c1", ttl=-1)
    store.create_access_token(conn, "h_live", "garmin", "me@x.cz", "c1", ttl=3600)
    store.cleanup_expired_tokens(conn)
    assert conn.execute("SELECT COUNT(*) FROM access_tokens").fetchone()[0] == 1


def test_revoke_account_and_token(conn):
    store.create_access_token(conn, "h1", "garmin", "me@x.cz", "c1")
    store.create_access_token(conn, "h2", "garmin", "me@x.cz", "c2")
    store.create_access_token(conn, "h3", "garmin", "other@x.cz", "c1")
    assert store.revoke_account(conn, "garmin", "me@x.cz") == 2
    assert store.account_key_for_token_hash(conn, "h1") is None
    assert store.account_key_for_token_hash(conn, "h3") == "other@x.cz"
    assert store.revoke_token(conn, "h3") == 1
    assert store.account_key_for_token_hash(conn, "h3") is None
    assert store.revoke_token(conn, "nope") == 0


def test_record_usage_counts(conn):
    store.record_usage(conn, "garmin", "me@x.cz", "get_activities")
    store.record_usage(conn, "garmin", "me@x.cz", "get_activities")
    store.record_usage(conn, "garmin", "me@x.cz", "tools/list")
    store.record_usage(conn, "garmin", "other@x.cz", "get_activities")
    rows = {r["tool"]: r["calls"] for r in conn.execute(
        "SELECT tool, calls FROM tool_usage WHERE account_key='me@x.cz'")}
    assert rows == {"get_activities": 2, "tools/list": 1}
    total = conn.execute("SELECT SUM(calls) FROM tool_usage WHERE tool='get_activities'").fetchone()[0]
    assert total == 3


def test_client_roundtrip(conn):
    store.create_client(conn, "c1", "secret_hash", ["https://a/cb", "https://b/cb"], "Claude", "garmin")
    c = store.get_client(conn, "c1")
    assert c["client_secret_hash"] == "secret_hash"
    assert c["redirect_uris"] == ["https://a/cb", "https://b/cb"]


def test_code_is_one_time(conn):
    store.create_code(conn, "ch", "c1", "https://a/cb", "challenge", "S256", "garmin", "me@x.cz")
    row = store.consume_code(conn, "ch")
    assert row["account_key"] == "me@x.cz"
    assert row["adapter"] == "garmin"
    assert row["code_challenge"] == "challenge"
    assert store.consume_code(conn, "ch") is None


def test_expired_code_returns_none(conn):
    store.create_code(conn, "ch", "c1", "https://a/cb", "x", "S256", "garmin", "me@x.cz", ttl=-1)
    assert store.consume_code(conn, "ch") is None


def test_list_accounts_returns_all(conn):
    store.upsert_account(conn, "garmin", "a@x.cz", "{}", SECRET)
    store.upsert_account(conn, "garmin", "b@x.cz", "{}", SECRET)
    rows = store.list_accounts(conn)
    keys = {r["account_key"] for r in rows}
    assert keys == {"a@x.cz", "b@x.cz"}
    assert all("created_at" in r and "updated_at" in r for r in rows)


def test_cleanup_expired_codes_removes_only_expired(conn):
    store.create_code(conn, "expired", "c1", "https://a/cb", "ch", "S256", "garmin", "me@x.cz", ttl=-1)
    store.create_code(conn, "valid", "c1", "https://a/cb", "ch", "S256", "garmin", "me@x.cz", ttl=600)
    store.cleanup_expired_codes(conn)
    remaining = conn.execute("SELECT code_hash FROM oauth_codes").fetchall()
    hashes = {r["code_hash"] for r in remaining}
    assert hashes == {"valid"}
```

- [ ] **Step 9: Update store call sites in `tests/test_oauth.py`**

- `_client_app`'s register handler must pass the adapter (test_oauth.py already defines `ADAPTER = GarminAdapter(CONFIG)`):
```python
def _client_app(conn):
    async def reg(request):
        return await oauth.register_client(request, conn, ADAPTER)
    return TestClient(Starlette(routes=[Route("/oauth/register", reg, methods=["POST"])]))
```
- `test_login_no_mfa_redirects_with_code`: the stored-tokens assertion becomes
  `assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) == '{"t":1}'`.
- `test_login_mfa_then_verify_redirects`: `assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) == '{"t":9}'`.
- `test_login_verify_failure_rerenders_form`: `assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) is None`.
- `test_mfa_verify_failure_restarts` (added in the adapter-seam fix): the non-persistence assert becomes `assert store.get_account_tokens(conn, "garmin", "me@x.cz", CONFIG.gateway_secret) is None`.
- `test_token_exchange_happy_path`: `store.upsert_account(conn, "garmin", "me@x.cz", "{}", CONFIG.gateway_secret)`; `store.create_code(conn, store.hash_token(code), cid, "https://claude.ai/cb", challenge, "S256", "garmin", "me@x.cz")`; the final assert stays `store.account_key_for_token_hash(conn, store.hash_token(token)) == "me@x.cz"`.
- `test_token_exchange_bad_pkce`: `store.create_code(conn, store.hash_token("c2"), cid, "https://claude.ai/cb", challenge, "S256", "garmin", "me@x.cz")`.

- [ ] **Step 10: Update store call sites in `tests/test_proxy.py`**

In `test_authorized_forwards_to_worker`:
```python
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "garmin", "me@x.cz", "c1")
```

- [ ] **Step 11: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: **88 passed** (87 baseline + 1 net new: 2 migration tests added, 0 removed — recount: baseline 87 + `test_migration_v0_to_v1_preserves_data` + `test_migration_is_idempotent` = **89 passed**).

If the count is not 89, investigate — a missed call site shows as a `TypeError` (wrong arg count) or `sqlite3.OperationalError: no such column: garmin_user_key`.

- [ ] **Step 12: Commit**

```bash
git add src/garmin_gateway/store.py src/garmin_gateway/oauth.py src/garmin_gateway/proxy.py src/garmin_gateway/app.py tests/test_store.py tests/test_oauth.py tests/test_proxy.py
git commit -m "refactor(store): adapter-aware schema + guarded v0->v1 migration

Generic accounts(adapter, account_key, blob_enc) + adapter column on
access_tokens/oauth_codes/oauth_clients/tool_usage; garmin_user_key renamed
account_key. One idempotent PRAGMA-user_version migration; ciphertext verbatim.
oauth/proxy thread the adapter into every store call; token-issued log field
renamed. Behavior otherwise identical (89 passed).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Adapter-isolation enforcement in `authenticate`

Now that tokens carry their adapter, the proxy must reject a Bearer minted for one adapter when used on another adapter's `/mcp`. Today (Garmin only) this is always a match, but it is the schema's purpose and future-proofs for a second adapter. Unit-tested with a synthetic cross-adapter token.

**Files:**
- Modify: `src/garmin_gateway/store.py` (`account_key_for_token_hash` returns a tuple)
- Modify: `src/garmin_gateway/proxy.py` (`authenticate` gains `adapter_name`, checks it)
- Modify: `tests/test_proxy.py` (call site + a rejection test)

**Interfaces produced:**
- `store.account_key_for_token_hash(conn, token_hash) -> tuple[str, str] | None` — `(adapter, account_key)` or `None`.
- `proxy.authenticate(request, adapter_name, conn, rate) -> str | Response` — returns `account_key` on success; 401 if the token's adapter ≠ `adapter_name`.

- [ ] **Step 1: Write the failing rejection test** (append to `tests/test_proxy.py`)

```python
def test_bearer_for_other_adapter_is_rejected(tmp_path, fake_worker):
    conn = store.init_db(":memory:")
    cfg = _cfg(tmp_path, fake_worker)
    token = "tok-rohlik"
    # a token minted for a DIFFERENT adapter
    store.upsert_account(conn, "rohlik", "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "rohlik", "me@x.cz", "c1")
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
    c = _app(conn, mgr, cfg)                     # _app forwards to the GARMIN adapter
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401                  # garmin path must not accept a rohlik token
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev pytest tests/test_proxy.py::test_bearer_for_other_adapter_is_rejected -v`
Expected: FAIL — currently the token is accepted (200), because `authenticate` ignores the token's adapter.

- [ ] **Step 3: Make `account_key_for_token_hash` return `(adapter, account_key)`** (`store.py`)

```python
def account_key_for_token_hash(conn, token_hash: str) -> "tuple[str, str] | None":
    row = conn.execute(
        "SELECT adapter, account_key, expires_at FROM access_tokens WHERE token_hash=?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and time.time() > row["expires_at"]:
        return None
    conn.execute(
        "UPDATE access_tokens SET last_used=datetime('now') WHERE token_hash=?",
        (token_hash,),
    )
    conn.commit()
    return (row["adapter"], row["account_key"])
```

- [ ] **Step 4: Enforce the adapter match in `proxy.authenticate`** and pass it from `handle_mcp`

`authenticate` (proxy.py ~line 32):
```python
async def authenticate(request, adapter_name, conn, rate) -> "str | Response":
    ip = request.client.host if request.client else "unknown"
    if not rate.check(f"unauth:{ip}", limit=30, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token_hash = store.hash_token(header[7:])
    if not rate.check(f"tok:{token_hash}", limit=60, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    found = store.account_key_for_token_hash(conn, token_hash)
    if found is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    tok_adapter, account_key = found
    if tok_adapter != adapter_name:                 # token belongs to another connector
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    return account_key
```

`handle_mcp` (proxy.py ~line 51) — pass `adapter.name`:
```python
    auth = await authenticate(request, adapter.name, conn, rate)
```

- [ ] **Step 5: Update tests that assert the old `account_key_for_token_hash` return shape**

The return type changed `str` → `tuple[str, str]`, so three store tests in `tests/test_store.py` (written in Task 1) now assert the wrong shape. Update them:

`test_access_token_maps_to_account`:
```python
    assert store.account_key_for_token_hash(conn, "hash1") == ("garmin", "me@x.cz")
    assert store.account_key_for_token_hash(conn, "nope") is None
```
`test_access_token_expiry`:
```python
    assert store.account_key_for_token_hash(conn, "h_exp") is None
    assert store.account_key_for_token_hash(conn, "h_live") == ("garmin", "me@x.cz")
    assert store.account_key_for_token_hash(conn, "h_forever") == ("garmin", "me@x.cz")
```
`test_revoke_account_and_token`:
```python
    assert store.account_key_for_token_hash(conn, "h1") is None
    assert store.account_key_for_token_hash(conn, "h3") == ("garmin", "other@x.cz")
    assert store.revoke_token(conn, "h3") == 1
    assert store.account_key_for_token_hash(conn, "h3") is None
```
Then confirm no test calls `proxy.authenticate` directly (there are none): `grep -n "proxy.authenticate" tests/` → no output. The `test_oauth.py` `test_token_exchange_happy_path` asserts `account_key_for_token_hash(...) == "me@x.cz"` too — update it to `== ("garmin", "me@x.cz")`.

- [ ] **Step 6: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: **90 passed** (89 + the rejection test; the three-plus-one assertion edits are not new tests).

- [ ] **Step 7: Commit**

```bash
git add src/garmin_gateway/store.py src/garmin_gateway/proxy.py tests/test_proxy.py
git commit -m "feat(proxy): reject a Bearer token used on a different adapter's /mcp

account_key_for_token_hash now returns (adapter, account_key); authenticate
compares it to the request's adapter. No-op for Garmin-only today, but the
schema's purpose and the isolation guarantee for a second adapter.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Update operator scripts to the new columns

The column rename broke every `scripts/*.py` that queries `garmin_user_key` / `garmin_accounts`. Update them so they keep working against the new schema. (The UX polish — unified overview, `--device` — is Plan B; this is keep-working only.)

**Files:**
- Modify: `scripts/status.py`, `scripts/revoke.py`, `scripts/usage.py`

**Interfaces:** none (operator CLIs; not imported by the app).

- [ ] **Step 1: Update `scripts/revoke.py`**

The listing query and the delete-by-account (lines ~51-52, ~62):
```python
            "SELECT account_key AS key, adapter, COUNT(*) AS n, MAX(last_used) AS last "
            "FROM access_tokens GROUP BY adapter, account_key ORDER BY adapter, account_key"
```
```python
        cur = conn.execute("DELETE FROM access_tokens WHERE account_key=?", (key,))
```

- [ ] **Step 2: Update `scripts/status.py`**

The counts and the per-account overview (lines ~48, ~50, ~65-69, ~82):
```python
    accounts = one("SELECT COUNT(*) FROM accounts")
```
```python
    people = one("SELECT COUNT(DISTINCT adapter || ':' || account_key) FROM access_tokens")
```
```python
        SELECT a.adapter AS adapter, a.account_key AS key, a.created_at AS created,
               COUNT(t.token_hash) AS devices, MAX(t.last_used) AS last
        FROM accounts a
        LEFT JOIN access_tokens t
          ON t.adapter = a.adapter AND t.account_key = a.account_key
        GROUP BY a.adapter, a.account_key ORDER BY a.created_at
```
```python
               GROUP_CONCAT(DISTINCT t.account_key) AS accounts
```
(Adjust surrounding print/format code only as needed to consume the renamed columns; keep the output shape.)

- [ ] **Step 3: Update `scripts/usage.py`**

Lines ~56, ~71, ~85-87:
```python
            "SELECT tool, calls, last_used FROM tool_usage WHERE account_key=? "
```
```python
            "SELECT tool, SUM(calls) AS n, COUNT(DISTINCT account_key) AS users "
```
```python
        "SELECT account_key AS key, SUM(calls) AS calls, "
        "MAX(last_used) AS last "
        "FROM tool_usage GROUP BY account_key ORDER BY calls DESC"
```

- [ ] **Step 4: Smoke-check each script against a seeded in-memory-style DB**

Create a throwaway DB, seed one account+token+usage via the store API, and run each script pointed at it. Run:
```bash
cd /Users/vaclav.slajs/dev/garmin-mcp-gateway
python3 - <<'PY'
from garmin_gateway import store
c = store.init_db("./.localdata/scripts-smoke.db")
store.upsert_account(c, "garmin", "me@x.cz", "{}", "k"*40)
store.create_access_token(c, store.hash_token("t"), "garmin", "me@x.cz", "c1")
store.record_usage(c, "garmin", "me@x.cz", "get_activities")
c.close()
print("seeded")
PY
DB_PATH=./.localdata/scripts-smoke.db python3 scripts/status.py
DB_PATH=./.localdata/scripts-smoke.db python3 scripts/revoke.py --list
DB_PATH=./.localdata/scripts-smoke.db python3 scripts/usage.py
rm -f ./.localdata/scripts-smoke.db*
```
Expected: each prints without a `sqlite3.OperationalError` and shows the `me@x.cz` account/usage. (Adjust the invocation flags to match each script's actual arg parser — the point is: no column errors.)

- [ ] **Step 5: Run the full test suite (scripts aren't tested, but confirm nothing else moved)**

Run: `uv run --extra dev pytest -q`
Expected: **90 passed**.

- [ ] **Step 6: Commit**

```bash
git add scripts/status.py scripts/revoke.py scripts/usage.py
git commit -m "fix(scripts): follow the accounts/account_key rename so status/revoke/usage keep working

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Docs — CLAUDE.md store bullet + spec/plan pointers

**Files:**
- Modify: `CLAUDE.md` (the `store.py` module bullet + the schema invariant)

**Interfaces:** none.

- [ ] **Step 1: Update the `store.py` bullet in CLAUDE.md**

Replace the `store.py` module bullet with:
```markdown
- **`store.py`** — SQLite schema + AES-256-GCM crypto + token hashing + CRUD, **adapter-keyed**. Tables: `accounts` (encrypted per-account blob, PK `(adapter, account_key)`), `access_tokens` (Bearer hash → `(adapter, account_key)`), `oauth_clients` (DCR, per adapter), `oauth_codes` (one-time PKCE, per adapter), `tool_usage` (per-account metrics). A guarded `PRAGMA user_version` 0→1 migration rewrites the pre-adapter Garmin schema in place (ciphertext verbatim). Encryption key = `SHA-256(GATEWAY_SECRET)`.
```

- [ ] **Step 2: Update the `garmin_user_key` invariant in "Cross-cutting invariants"**

Replace the `garmin_user_key` bullet with:
```markdown
- **`account_key`** = the normalized **lowercased login email**, scoped by `adapter`. `(adapter, account_key)` is the join key across every table *and* (with `account_key` alone) the worker registry. A Bearer token carries its `adapter`; the proxy rejects a token used on a different adapter's `/mcp`.
```

- [ ] **Step 3: Run the full suite one last time**

Run: `uv run --extra dev pytest -q`
Expected: **90 passed**.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: adapter-keyed store in CLAUDE.md module map + invariants

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Out of scope (later)

- **Path routing `/garmin/mcp` + path-scoped `.well-known`** — Plan A2 (ends with the staging discovery spike).
- **Revoke/status UX polish, home page, Litestream backups** — Plan B.
- Worker-registry composite `(adapter, account_key)` keying — deferred until a second adapter exists.

## Verification at the end

1. `uv run --extra dev pytest -q` → **90 passed**.
2. Migration sanity on the real staging DB (the rehearsal for prod): after deploying this plan to staging, confirm the one spike-test account survived — `railway ssh --service gateway "python3 scripts/status.py"` lists `garmin / vaclav@slajs.eu` and the connector still works (a tool call succeeds). This is the migration's real-world gate; the URL is unchanged in this plan, so the existing staging connector keeps working.
3. `git log --oneline` shows 4 commits, each with a green suite behind it.

## Self-review notes (author)

- Spec Part 1 coverage: generic `accounts` ✓ (Task 1), `adapter` columns on the four tables ✓ (Task 1), guarded idempotent migration + ciphertext verbatim ✓ (Task 1 + tests), `garmin_user_key`→`account_key` ✓, scripts kept working ✓ (Task 3), the one log-field change flagged ✓. Adapter isolation (schema's purpose) ✓ (Task 2).
- Test-count arithmetic: baseline 87 → Task 1 +2 (89) → Task 2 +1 (90) → Tasks 3–4 +0 (90).
- Type consistency: `account_key_for_token_hash` returns `str` in Task 1, `tuple[str, str]` in Task 2. The Task-1 store tests assert the `str` shape and pass in Task 1; Task 2 **Step 5** explicitly updates those assertions (`test_access_token_maps_to_account`, `test_access_token_expiry`, `test_revoke_account_and_token`, and `test_oauth.py::test_token_exchange_happy_path`) to the tuple shape. No dangling `== "me@x.cz"` assertion survives Task 2.
```
