import time
import pytest
from missingmcp import store

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
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', SECRET)
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", SECRET) == '{"t":1}'
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":2}', SECRET)
    assert store.get_account_tokens(conn, "garmin", "me@x.cz", SECRET) == '{"t":2}'
    assert store.get_account_tokens(conn, "garmin", "absent@x.cz", SECRET) is None


def test_access_token_maps_to_account(conn):
    store.upsert_account(conn, "garmin", "me@x.cz", "{}", SECRET)
    store.create_access_token(conn, "hash1", "garmin", "me@x.cz", "client1")
    assert store.account_key_for_token_hash(conn, "hash1") == ("garmin", "me@x.cz")
    assert store.account_key_for_token_hash(conn, "nope") is None


def test_access_token_expiry(conn):
    store.create_access_token(conn, "h_exp", "garmin", "me@x.cz", "c1", ttl=-1)    # already expired
    store.create_access_token(conn, "h_live", "garmin", "me@x.cz", "c1", ttl=3600)
    store.create_access_token(conn, "h_forever", "garmin", "me@x.cz", "c1")        # ttl=0 -> no expiry
    assert store.account_key_for_token_hash(conn, "h_exp") is None       # rejected
    assert store.account_key_for_token_hash(conn, "h_live") == ("garmin", "me@x.cz")
    assert store.account_key_for_token_hash(conn, "h_forever") == ("garmin", "me@x.cz")


def test_cleanup_expired_tokens(conn):
    store.create_access_token(conn, "h_exp", "garmin", "me@x.cz", "c1", ttl=-1)
    store.create_access_token(conn, "h_live", "garmin", "me@x.cz", "c1", ttl=3600)
    store.cleanup_expired_tokens(conn)
    assert conn.execute("SELECT COUNT(*) FROM access_tokens").fetchone()[0] == 1  # only h_live


def test_revoke_account_and_token(conn):
    store.create_access_token(conn, "h1", "garmin", "me@x.cz", "c1")
    store.create_access_token(conn, "h2", "garmin", "me@x.cz", "c2")
    store.create_access_token(conn, "h3", "garmin", "other@x.cz", "c1")
    assert store.revoke_account(conn, "garmin", "me@x.cz") == 2
    assert store.account_key_for_token_hash(conn, "h1") is None
    assert store.account_key_for_token_hash(conn, "h3") == ("garmin", "other@x.cz")   # untouched
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
    assert store.consume_code(conn, "ch") is None  # already consumed


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


def test_migration_rolls_back_and_stays_remigratable(tmp_path, monkeypatch):
    """A crash mid-migration must roll back atomically (no orphan `accounts`
    table) and leave the DB re-migratable on the next init_db."""
    db = str(tmp_path / "old.db")
    _build_v0_db(db)
    # Inject a failing statement partway through the migration.
    broken = list(store._MIGRATE_V1) + ["THIS IS NOT VALID SQL"]
    monkeypatch.setattr(store, "_MIGRATE_V1", broken)
    with pytest.raises(Exception):
        store.init_db(db).close()
    # Reopen with the real (unpatched) migration list: it must succeed, proving
    # the failed attempt left no orphan `accounts` table and the v0 data intact.
    monkeypatch.undo()
    conn = store.init_db(db)
    row = conn.execute("SELECT adapter, account_key, blob_enc FROM accounts").fetchone()
    assert (row["adapter"], row["account_key"], row["blob_enc"]) == ("garmin", "me@x.cz", "NONCE:CIPHER")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()
