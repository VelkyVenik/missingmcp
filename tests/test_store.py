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


def test_list_accounts_returns_all(conn):
    store.upsert_account(conn, "a@x.cz", "{}", SECRET)
    store.upsert_account(conn, "b@x.cz", "{}", SECRET)
    rows = store.list_accounts(conn)
    keys = {r["garmin_user_key"] for r in rows}
    assert keys == {"a@x.cz", "b@x.cz"}
    assert all("created_at" in r and "updated_at" in r for r in rows)


def test_cleanup_expired_codes_removes_only_expired(conn):
    store.create_code(conn, "expired", "c1", "https://a/cb", "ch", "S256", "me@x.cz", ttl=-1)
    store.create_code(conn, "valid", "c1", "https://a/cb", "ch", "S256", "me@x.cz", ttl=600)
    store.cleanup_expired_codes(conn)
    remaining = conn.execute("SELECT code_hash FROM oauth_codes").fetchall()
    hashes = {r["code_hash"] for r in remaining}
    assert hashes == {"valid"}
