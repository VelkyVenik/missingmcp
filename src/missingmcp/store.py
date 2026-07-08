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
CREATE TABLE IF NOT EXISTS subscribers (
    email      TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    wants_updates INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
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
        # Explicit transaction: sqlite3 only auto-opens one before DML, so the
        # leading CREATE TABLE (DDL) would otherwise auto-commit outside any
        # rollback scope. BEGIN makes the whole migration atomic — a crash
        # mid-migration rolls back cleanly and the DB stays re-migratable.
        conn.execute("BEGIN")
        try:
            for stmt in _MIGRATE_V1:
                conn.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
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
    """Aggregate counts for monitoring: accounts, issued tokens, distinct people
    holding a token, registered OAuth clients, pending auth codes."""
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

# Access tokens have no TTL and are not auto-revoked. To revoke a device,
# delete its row from the access_tokens table (admin is DB-level; UI deferred per design).
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


def account_key_for_token_hash(conn, token_hash: str) -> "tuple[str, str] | None":
    row = conn.execute(
        "SELECT adapter, account_key, expires_at FROM access_tokens WHERE token_hash=?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] is not None and time.time() > row["expires_at"]:
        return None  # expired; the reaper purges it
    conn.execute(
        "UPDATE access_tokens SET last_used=datetime('now') WHERE token_hash=?",
        (token_hash,),
    )
    conn.commit()
    return (row["adapter"], row["account_key"])


def cleanup_expired_tokens(conn) -> None:
    conn.execute(
        "DELETE FROM access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
        (int(time.time()),),
    )
    conn.commit()


def revoke_token(conn, token_hash: str) -> int:
    """Revoke one access token by its hash. Returns rows deleted."""
    cur = conn.execute("DELETE FROM access_tokens WHERE token_hash=?", (token_hash,))
    conn.commit()
    return cur.rowcount


def revoke_account(conn, adapter: str, account_key: str) -> int:
    """Revoke all access tokens for an account (a 'log out all devices'). Returns
    rows deleted. The account row itself is left intact."""
    cur = conn.execute(
        "DELETE FROM access_tokens WHERE adapter=? AND account_key=?",
        (adapter, account_key),
    )
    conn.commit()
    return cur.rowcount


def delete_account(conn, adapter: str, account_key: str) -> int:
    """Delete an account's stored (encrypted) credential blob — e.g. when the
    upstream revoked the app's access, dead tokens must not linger (WHOOP API
    Terms of Use: delete stored content on termination). Access tokens are
    revoked separately via revoke_account. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM accounts WHERE adapter=? AND account_key=?",
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


def cleanup_orphan_clients(conn, older_than_seconds: int) -> int:
    """Delete OAuth clients that never produced an access token and are older than
    the cutoff — abandoned or failed DCR registrations (Claude registers a fresh
    client per connection attempt). The age guard keeps an in-flight OAuth flow
    (client registered, token not yet issued) safe. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM oauth_clients WHERE created_at < datetime('now', ?) "
        "AND NOT EXISTS (SELECT 1 FROM access_tokens t WHERE t.client_id = oauth_clients.client_id)",
        (f"-{int(older_than_seconds)} seconds",),
    )
    conn.commit()
    return cur.rowcount


def purge_adapter(conn, adapter: str) -> dict:
    """Delete every row belonging to a retired adapter, across all tables — full
    off-boarding for an adapter we no longer serve (see adapters.RETIRED_ADAPTERS
    and docs/adr/0001). Returns per-table deletion counts."""
    counts = {}
    for table in ("accounts", "access_tokens", "oauth_clients", "oauth_codes", "tool_usage"):
        cur = conn.execute(f"DELETE FROM {table} WHERE adapter=?", (adapter,))
        counts[table] = cur.rowcount
    conn.commit()
    return counts


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

# Records only the tool/method name and a per-account count — never request
# contents or any Garmin data.
def record_usage(conn, adapter: str, account_key: str, tool: str) -> None:
    conn.execute(
        "INSERT INTO tool_usage (adapter, account_key, tool, calls, last_used) "
        "VALUES (?, ?, ?, 1, datetime('now')) "
        "ON CONFLICT(adapter, account_key, tool) DO UPDATE SET "
        "calls = calls + 1, last_used = datetime('now')",
        (adapter, account_key, tool),
    )
    conn.commit()


# --- newsletter subscribers & connector suggestions -----------------------
# Marketing opt-in captured on the home page. Stored locally only — no email is
# sent from here (a provider is chosen later). Never log the address itself.

def add_subscriber(conn, email: str) -> None:
    """Record a newsletter opt-in. Idempotent: a repeat email is a silent no-op
    (INSERT OR IGNORE), so the endpoint can't be used to probe who's subscribed."""
    conn.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
    conn.commit()


def add_suggestion(conn, email: str, description: str, wants_updates: bool) -> None:
    """Record a 'which connector next?' suggestion (a log — repeats allowed). When
    wants_updates, the email is also added to the newsletter list."""
    conn.execute(
        "INSERT INTO suggestions (email, description, wants_updates) VALUES (?, ?, ?)",
        (email, description, 1 if wants_updates else 0),
    )
    if wants_updates:
        conn.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
    conn.commit()


def list_subscribers(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT email, created_at FROM subscribers ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def list_suggestions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT email, description, wants_updates, created_at "
        "FROM suggestions ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]
