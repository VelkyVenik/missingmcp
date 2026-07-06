import datetime
import hashlib
import json
import re
import sqlite3
import pytest
from garmin_gateway import backup, store
from garmin_gateway.config import load_config

SECRET = "s" * 40


def _cfg(tmp_path, extra=None):
    env = {"GATEWAY_SECRET": SECRET, "PUBLIC_URL": "https://x",
           "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "gw.db")}
    env.update(extra or {})
    return load_config(env)


def _s3_env(fake_remote):
    return {
        "BACKUP_S3_ENDPOINT": f"http://127.0.0.1:{fake_remote.port}",
        "BACKUP_S3_BUCKET": "bkt",
        "BACKUP_S3_ACCESS_KEY": "AKtest",
        "BACKUP_S3_SECRET_KEY": "SKtest",
        "BACKUP_S3_URL_STYLE": "path",
    }


# --- config surface ---

def test_backups_disabled_without_credentials(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.backup_s3_endpoint == "" and cfg.backup_s3_bucket == ""
    assert not backup.Backup(cfg).enabled


def test_backup_config_parsed(tmp_path):
    cfg = _cfg(tmp_path, {"BACKUP_S3_ENDPOINT": "https://t3.storageapi.dev",
                          "BACKUP_S3_BUCKET": "b", "BACKUP_S3_ACCESS_KEY": "a",
                          "BACKUP_S3_SECRET_KEY": "s", "BACKUP_S3_REGION": "auto",
                          "BACKUP_INTERVAL_HOURS": "12"})
    assert cfg.backup_s3_region == "auto"
    assert cfg.backup_interval == 12 * 3600
    assert cfg.backup_s3_url_style == "virtual-host"   # prod default
    assert backup.Backup(cfg).enabled


def test_backup_interval_default_six_hours(tmp_path):
    assert _cfg(tmp_path).backup_interval == 6 * 3600


# --- snapshot ---

def test_snapshot_is_a_consistent_sqlite_copy(tmp_path):
    cfg = _cfg(tmp_path)
    conn = store.init_db(cfg.db_path)
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', SECRET)
    body = backup.snapshot_db(cfg.db_path)

    restored = tmp_path / "restored.db"
    restored.write_bytes(body)
    rc = sqlite3.connect(str(restored))
    assert rc.execute("PRAGMA user_version").fetchone()[0] == 1
    rows = rc.execute("SELECT adapter, account_key FROM accounts").fetchall()
    assert rows == [("garmin", "me@x.cz")]
    # the copy must be self-contained (no dangling WAL dependency)
    assert rc.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --- SigV4 / S3 PUT ---

def test_sigv4_headers_shape():
    now = datetime.datetime(2026, 7, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)
    payload_hash = hashlib.sha256(b"hello").hexdigest()
    h = backup.sigv4_headers(method="PUT", host="bkt.t3.storageapi.dev",
                             path="/db/gateway-mon.db", region="auto",
                             access_key="AK", secret_key="SK",
                             payload_hash=payload_hash, now=now)
    assert h["x-amz-date"] == "20260706T120000Z"
    assert h["x-amz-content-sha256"] == payload_hash
    m = re.fullmatch(
        r"AWS4-HMAC-SHA256 Credential=AK/20260706/auto/s3/aws4_request, "
        r"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}",
        h["authorization"])
    assert m, h["authorization"]


def test_sigv4_signature_is_deterministic():
    now = datetime.datetime(2026, 7, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)
    kw = dict(method="PUT", host="h", path="/k", region="auto",
              access_key="AK", secret_key="SK",
              payload_hash=hashlib.sha256(b"x").hexdigest(), now=now)
    assert backup.sigv4_headers(**kw) == backup.sigv4_headers(**kw)
    changed = backup.sigv4_headers(**{**kw, "secret_key": "OTHER"})
    assert changed["authorization"] != backup.sigv4_headers(**kw)["authorization"]


def test_s3_put_path_style_sends_signed_request(fake_remote):
    body = b"backup-bytes"
    backup.s3_put(endpoint=f"http://127.0.0.1:{fake_remote.port}", bucket="bkt",
                  region="auto", access_key="AK", secret_key="SK",
                  key="db/gateway-mon.db", body=body, url_style="path")
    method, path, headers, got = fake_remote.calls[-1]
    assert (method, path) == ("PUT", "/bkt/db/gateway-mon.db")
    assert got == body
    assert headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")


def test_s3_put_raises_on_error_status(fake_remote):
    fake_remote.response_status = 403
    with pytest.raises(backup.BackupError):
        backup.s3_put(endpoint=f"http://127.0.0.1:{fake_remote.port}", bucket="bkt",
                      region="auto", access_key="AK", secret_key="SK",
                      key="k", body=b"x", url_style="path")


# --- orchestration ---

def test_backup_run_uploads_weekday_snapshot(tmp_path, fake_remote):
    cfg = _cfg(tmp_path, _s3_env(fake_remote))
    conn = store.init_db(cfg.db_path)
    store.upsert_account(conn, "garmin", "me@x.cz", '{"t":1}', SECRET)

    bk = backup.Backup(cfg)
    assert bk.enabled and bk.due()
    bk.run()

    method, path, _hdrs, body = fake_remote.calls[-1]
    weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][
        datetime.datetime.now(datetime.timezone.utc).weekday()]
    assert (method, path) == ("PUT", f"/bkt/db/gateway-{weekday}.db")
    assert body[:16] == b"SQLite format 3\x00"
    assert not bk.due()                      # next run scheduled a full interval away


def test_backup_run_failure_is_swallowed_and_rescheduled(tmp_path, fake_remote):
    fake_remote.response_status = 500
    cfg = _cfg(tmp_path, _s3_env(fake_remote))
    store.init_db(cfg.db_path)
    bk = backup.Backup(cfg)
    bk.run()                                 # must not raise (lifespan loop calls this)
    assert not bk.due()
