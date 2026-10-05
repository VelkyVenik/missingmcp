from __future__ import annotations
import hashlib
import json
import os
import secrets
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
    created_at         TEXT DEFAULT (datetime('now')),
    last_seen          TEXT DEFAULT (datetime('now'))
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
CREATE TABLE IF NOT EXISTS beers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT,                       -- supporter email, lowercased; NULL if anonymous
    beers       INTEGER NOT NULL DEFAULT 1, -- coffee count for this donation
    amount      REAL,                       -- money value (beers x unit price)
    currency    TEXT,
    matched     INTEGER NOT NULL DEFAULT 0, -- 1 if email matched an account_key
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT DEFAULT (datetime('now'))  -- purchase time (--at, else now)
);
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,              -- plain text, snapshotted at create
    audience    TEXT NOT NULL,              -- all | users | subscribers
    status      TEXT NOT NULL DEFAULT 'draft',  -- draft | active | paused | done
    created_at  TEXT DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS campaign_sends (
    campaign_id INTEGER NOT NULL,
    email       TEXT NOT NULL,
    rank        INTEGER NOT NULL,           -- send order: most active users first
    status      TEXT NOT NULL DEFAULT 'pending',
    unsub_token TEXT NOT NULL UNIQUE,
    message_id  TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    sent_at     TEXT,                       -- set once the API accepted it (sent|bounced)
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (campaign_id, email)
);
CREATE TABLE IF NOT EXISTS unsubscribes (
    email      TEXT PRIMARY KEY,
    source     TEXT NOT NULL,               -- link | one-click | manual
    created_at TEXT DEFAULT (datetime('now'))
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
    PRAGMA user_version; idempotent. Fresh DBs (no tables yet) are just stamped
    — _SCHEMA creates them in their current shape afterwards."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
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
    if version < 2:
        # v2: oauth_clients.last_seen — the orphan sweep keys on last activity,
        # not creation age (oauth-client-lifecycle §1 / reliability ticket 04).
        # Existing rows seed last_seen from created_at so a just-migrated DB
        # neither insta-sweeps everything nor keeps NULLs forever.
        has_clients = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oauth_clients'"
        ).fetchone() is not None
        if has_clients:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(oauth_clients)")}
            if "last_seen" not in cols:
                conn.execute("ALTER TABLE oauth_clients ADD COLUMN last_seen TEXT")
                conn.execute("UPDATE oauth_clients SET last_seen = created_at "
                             "WHERE last_seen IS NULL")
        conn.execute("PRAGMA user_version = 2")
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


def account_exists(conn, adapter: str, account_key: str) -> bool:
    """Cheap existence probe (no decrypt) — telemetry's new|returning signal,
    checked before upsert_account overwrites the row."""
    return conn.execute(
        "SELECT 1 FROM accounts WHERE adapter=? AND account_key=?",
        (adapter, account_key),
    ).fetchone() is not None


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


# --- daily user-stats (report.py) -----------------------------------------
# created_at / last_used are UTC strings in SQLite's datetime('now') format
# ("YYYY-MM-DD HH:MM:SS"), so callers pass UTC bounds in the same format and
# lexicographic comparison == chronological comparison.

def new_accounts_between(conn, start_utc: str, end_utc: str) -> dict:
    """Accounts created in [start_utc, end_utc), grouped by adapter."""
    rows = conn.execute(
        "SELECT adapter, COUNT(*) FROM accounts "
        "WHERE created_at >= ? AND created_at < ? GROUP BY adapter",
        (start_utc, end_utc),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# JSON-RPC methods an MCP client sends on its own — handshake and discovery, not
# a user asking for anything. `record_usage` stores them under `tool` like any
# real call (proxy._mcp_tool returns the bare method for everything that isn't
# `tools/call`), so any *activity* metric has to exclude them. Counting them
# reports a merely-connected client as an active user: on 2026-07-25 that was 83
# "active" accounts against 48 that actually invoked a tool, because Claude
# re-runs the handshake on every connector refresh.
PROTOCOL_METHODS = frozenset({
    "initialize",
    "notifications/initialized",
    "notifications/cancelled",
    "ping",
    "tools/list",
    "tools/call",            # only reached when the call carried no tool name
    "resources/list",
    "resources/templates/list",
    "resources/subscribe",
    "resources/unsubscribe",
    "prompts/list",
    "logging/setLevel",
    "completion/complete",
})


def active_accounts_between(conn, start_utc: str, end_utc: str) -> dict:
    """Distinct accounts that invoked a tool in [start_utc, end_utc), by adapter.

    Protocol traffic (`PROTOCOL_METHODS`) is excluded — see that constant for why.
    Note this still reads `tool_usage.last_used`, which `record_usage` overwrites
    per (adapter, account_key, tool): an account that called the same tool again
    later falls out of the earlier window, so the count remains a lower bound."""
    placeholders = ",".join("?" * len(PROTOCOL_METHODS))
    rows = conn.execute(
        "SELECT adapter, COUNT(DISTINCT account_key) FROM tool_usage "
        "WHERE last_used >= ? AND last_used < ? "
        f"AND tool NOT IN ({placeholders}) GROUP BY adapter",
        (start_utc, end_utc, *sorted(PROTOCOL_METHODS)),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def total_accounts_by_adapter(conn) -> dict:
    """Cumulative account count per adapter."""
    rows = conn.execute(
        "SELECT adapter, COUNT(*) FROM accounts GROUP BY adapter"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


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
    # Every authorize/token use flows through here — stamp last_seen on read
    # (the account_key_for_token_hash/last_used idiom), so a long-idle but
    # still-cached client (Claude re-uses a DCR registration for months) never
    # ages into the orphan sweep while it is actually being used.
    conn.execute("UPDATE oauth_clients SET last_seen=datetime('now') WHERE client_id=?",
                 (client_id,))
    conn.commit()
    return {
        "client_secret_hash": row["client_secret_hash"],
        "redirect_uris": json.loads(row["redirect_uris"]),
    }


def cleanup_orphan_clients(conn, older_than_seconds: int) -> int:
    """Delete OAuth clients with no live access token that have not been USED
    (last_seen, stamped by get_client on every authorize/token use) within the
    cutoff — abandoned DCR registrations (directory scanners register clients
    they never use). Keyed on activity, not creation age: real clients (Claude,
    ChatGPT) CACHE their registration per org and re-use it for months, and
    sweeping a cached-but-active client strands its users on "unknown
    client_id" until they remove + re-add the connector
    (oauth-client-lifecycle §1). The cutoff still must be generous (days, not
    hours). Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM oauth_clients "
        "WHERE COALESCE(last_seen, created_at) < datetime('now', ?) "
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
# Marketing opt-in captured on the home page. Stored locally; mail goes out only
# through an operator-started campaign (mailer.py). Never log the address itself.

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


# --- beer supporters ------------------------------------------------------
# Local audit/record of "buy me a beer" donations, entered manually (scripts/
# add_beer.py). Feeds no website counter — the operator's numbers live in
# PostHog (the beer_purchased event). See the 2026-07-24 beer-supporters spec.

def account_key_exists(conn, email: str) -> bool:
    """True if the (normalized) email is a login account_key under any adapter —
    the best-effort attribution probe for a beer donation. Same identity the
    connect funnel keys on (distinct_id = plain login email)."""
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_key = ? LIMIT 1", (email,)
    ).fetchone() is not None


def add_beer(conn, *, email: str | None, beers: int, amount: float | None,
             currency: str | None, matched: int, source: str,
             created_at: str) -> int:
    """Insert one donation row; returns the new row id (used to build the
    anonymous synthetic distinct_id, `manual:anon:<id>`)."""
    cur = conn.execute(
        "INSERT INTO beers (email, beers, amount, currency, matched, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (email, beers, amount, currency, matched, source, created_at),
    )
    conn.commit()
    return cur.lastrowid


# --- mail campaigns (mailer.py) -------------------------------------------
# One row per (campaign, recipient) is the send ledger: a recipient is mailed at
# most once per campaign, and `sending` is written BEFORE the API call, so a
# crash mid-call leaves an explicit `unknown` (never an automatic re-send).
# Unsubscribes are global — they exclude the address from every campaign.

SEND_STATUSES = ("pending", "sending", "sent", "bounced", "suppressed",
                 "failed", "unknown", "unsubscribed")


def campaign_audience(conn, audience: str) -> list[str]:
    """Recipients for a new campaign, most engaged first: latest real tool use
    (protocol traffic excluded, like active_accounts_between), then total
    calls; never-active accounts and subscriber-only addresses go last.
    `audience` is all | users (account emails) | subscribers. Unsubscribed
    addresses are excluded."""
    sources = {
        "users": "SELECT account_key AS email FROM accounts",
        "subscribers": "SELECT email FROM subscribers",
        "all": "SELECT account_key AS email FROM accounts UNION SELECT email FROM subscribers",
    }[audience]
    placeholders = ",".join("?" * len(PROTOCOL_METHODS))
    rows = conn.execute(
        f"WITH people AS ({sources}), "
        "activity AS (SELECT account_key AS email, MAX(last_used) AS last_active, "
        "             SUM(calls) AS calls FROM tool_usage "
        f"            WHERE tool NOT IN ({placeholders}) GROUP BY account_key) "
        "SELECT DISTINCT p.email, a.last_active, COALESCE(a.calls, 0) AS calls "
        "FROM people p LEFT JOIN activity a ON a.email = p.email "
        "WHERE p.email NOT IN (SELECT email FROM unsubscribes) "
        "ORDER BY a.last_active IS NULL, a.last_active DESC, calls DESC, p.email",
        tuple(sorted(PROTOCOL_METHODS)),
    ).fetchall()
    return [r[0] for r in rows]


def create_campaign(conn, slug: str, subject: str, body: str, audience: str,
                    recipients: list[str]) -> int:
    """Create a draft campaign and enqueue `recipients` (in send order) as
    pending. Each send gets its own random unsubscribe token. Atomic."""
    try:
        cur = conn.execute(
            "INSERT INTO campaigns (slug, subject, body, audience) VALUES (?, ?, ?, ?)",
            (slug, subject, body, audience))
        cid = cur.lastrowid
        conn.executemany(
            "INSERT INTO campaign_sends (campaign_id, email, rank, unsub_token) "
            "VALUES (?, ?, ?, ?)",
            [(cid, email, i, secrets.token_urlsafe(24))
             for i, email in enumerate(recipients)])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cid


def get_campaign(conn, slug: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM campaigns WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def list_campaigns(conn) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM campaigns ORDER BY id")]


def set_campaign_status(conn, campaign_id: int, status: str) -> None:
    stamp = {"active": ", started_at = COALESCE(started_at, datetime('now'))",
             "done": ", finished_at = datetime('now')"}.get(status, "")
    conn.execute(f"UPDATE campaigns SET status=?{stamp} WHERE id=?", (status, campaign_id))
    conn.commit()


def active_campaign(conn) -> dict | None:
    """The campaign the mailer works on: the earliest-started active one."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM campaigns WHERE status='active' "
                       "ORDER BY started_at, id LIMIT 1").fetchone()
    return dict(row) if row else None


def next_pending(conn, campaign_id: int, limit: int) -> list[dict]:
    """The next sends, in rank order. An address that unsubscribed after it
    was (re)queued is dropped here, right before sending — the one choke point
    every path to the API goes through (first send, retry, operator requeue)."""
    conn.execute("UPDATE campaign_sends SET status='unsubscribed', updated_at=datetime('now') "
                 "WHERE campaign_id=? AND status='pending' "
                 "AND email IN (SELECT email FROM unsubscribes)", (campaign_id,))
    conn.commit()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT email, unsub_token, attempts FROM campaign_sends "
        "WHERE campaign_id=? AND status='pending' ORDER BY rank LIMIT ?",
        (campaign_id, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_send(conn, campaign_id: int, email: str, status: str, *,
              message_id: str | None = None, error: str | None = None,
              attempted: bool = False, at: str | None = None) -> None:
    """Move one ledger row to `status`. `sent_at` is stamped (with `at`, a UTC
    'YYYY-MM-DD HH:MM:SS', else now) when the message counts toward the daily
    quota: accepted (sent / bounced) or possibly accepted (unknown — counting
    it errs on the side of staying under the provider's quota).
    `attempted` bumps the retry counter."""
    conn.execute(
        "UPDATE campaign_sends SET status=?, "
        "message_id=COALESCE(?, message_id), last_error=?, "
        "attempts=attempts + ?, updated_at=datetime('now'), "
        "sent_at=CASE WHEN ? IN ('sent','bounced','unknown') "
        "THEN COALESCE(?, datetime('now')) ELSE sent_at END "
        "WHERE campaign_id=? AND email=?",
        (status, message_id, error, 1 if attempted else 0, status, at, campaign_id, email))
    conn.commit()


def mark_stale_sending(conn) -> int:
    """A `sending` row outside a running send is a crash mid-API-call: the mail
    may or may not have gone out. Park it as `unknown` (operator decides), and
    count it toward the quota like any maybe-sent: sent_at = when it was marked
    `sending` (its updated_at), unless already stamped."""
    cur = conn.execute("UPDATE campaign_sends SET status='unknown', "
                       "last_error='interrupted mid-send', "
                       "sent_at=COALESCE(sent_at, updated_at), updated_at=datetime('now') "
                       "WHERE status='sending'")
    conn.commit()
    return cur.rowcount


def sent_since(conn, since_utc: str) -> int:
    """Mails the API accepted since `since_utc` (all campaigns) — the quota
    gauge, over a rolling 24h window."""
    return conn.execute("SELECT COUNT(*) FROM campaign_sends WHERE sent_at >= ?",
                        (since_utc,)).fetchone()[0]


def oldest_sent_since(conn, since_utc: str) -> str | None:
    """The earliest sent_at inside the window — when it ages out of the
    rolling 24h quota window, room opens up again."""
    return conn.execute("SELECT MIN(sent_at) FROM campaign_sends WHERE sent_at >= ?",
                        (since_utc,)).fetchone()[0]


def campaign_counts(conn, campaign_id: int) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) FROM campaign_sends "
                        "WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()
    counts = {s: 0 for s in SEND_STATUSES}
    counts.update({r[0]: r[1] for r in rows})
    return counts


def requeue(conn, campaign_id: int, status: str) -> int:
    """Operator decision after reviewing `unknown` or `failed` rows: send them
    again (attempts reset). Unsubscribed addresses are still dropped at send
    time by next_pending."""
    if status not in ("unknown", "failed"):
        raise ValueError(f"cannot requeue {status!r} sends")
    cur = conn.execute("UPDATE campaign_sends SET status='pending', attempts=0, "
                       "updated_at=datetime('now') WHERE campaign_id=? AND status=?",
                       (campaign_id, status))
    conn.commit()
    return cur.rowcount


def unknown_sends(conn, campaign_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT email FROM campaign_sends WHERE campaign_id=? AND status='unknown' "
        "ORDER BY rank", (campaign_id,))]


def add_unsubscribe(conn, email: str, source: str) -> None:
    """Global opt-out: recorded once, and every send to the address that could
    still go out (pending, or unknown/failed awaiting an operator requeue) is
    dropped from its campaign. Idempotent."""
    conn.execute("INSERT OR IGNORE INTO unsubscribes (email, source) VALUES (?, ?)",
                 (email, source))
    conn.execute("UPDATE campaign_sends SET status='unsubscribed', updated_at=datetime('now') "
                 "WHERE email=? AND status IN ('pending', 'unknown', 'failed')", (email,))
    conn.commit()


def email_for_unsub_token(conn, token: str) -> str | None:
    row = conn.execute("SELECT email FROM campaign_sends WHERE unsub_token=?",
                       (token,)).fetchone()
    return row[0] if row else None
