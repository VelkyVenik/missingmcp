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
