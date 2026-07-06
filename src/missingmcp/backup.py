"""Off-box SQLite backups: a periodic consistent snapshot uploaded to an
S3-compatible bucket (Railway bucket / Tigris). Deliberately dependency-free:
a minimal AWS SigV4 signer over httpx instead of boto3.

Key layout: db/gateway-<weekday>.db — seven rotating slots, overwritten
weekly, so retention needs no list/delete logic. A backup restores a working
gateway only together with GATEWAY_SECRET (blobs stay AES-256-GCM encrypted);
keep the secret somewhere that is NOT this bucket. See README → Backups.
"""
from __future__ import annotations
import datetime
import hashlib
import hmac
import os
import sqlite3
import tempfile
import time
from urllib.parse import quote, urlsplit
import httpx
from .log import log, log_exc

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class BackupError(Exception):
    pass


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(*, method: str, host: str, path: str, region: str,
                  access_key: str, secret_key: str, payload_hash: str,
                  now: datetime.datetime | None = None) -> dict[str, str]:
    """AWS Signature v4 for a query-less request with UNSIGNED body hash header.
    Signed headers: host, x-amz-content-sha256, x-amz-date."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    canonical_uri = quote(path, safe="/")
    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{payload_hash}\n"
                         f"x-amz-date:{amz_date}\n")
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (f"{method}\n{canonical_uri}\n\n"
                         f"{canonical_headers}\n{signed}\n{payload_hash}")
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = ("AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n"
                      + hashlib.sha256(canonical_request.encode()).hexdigest())
    k_signing = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(),
                                        datestamp), region), "s3"), "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                          f"SignedHeaders={signed}, Signature={signature}"),
    }


def s3_put(*, endpoint: str, bucket: str, region: str, access_key: str,
           secret_key: str, key: str, body: bytes, url_style: str) -> None:
    """Blocking PUT of one object. Raises BackupError on any failure."""
    scheme, netloc = urlsplit(endpoint)[:2]
    if url_style == "virtual-host":
        host, path = f"{bucket}.{netloc}", f"/{key}"
    else:  # "path" — used by tests (a fake server can't serve bucket.127.0.0.1)
        host, path = netloc, f"/{bucket}/{key}"
    headers = sigv4_headers(method="PUT", host=host, path=path, region=region,
                            access_key=access_key, secret_key=secret_key,
                            payload_hash=hashlib.sha256(body).hexdigest())
    try:
        resp = httpx.put(f"{scheme}://{host}{path}", content=body,
                         headers=headers, timeout=60.0)
    except httpx.HTTPError as e:
        raise BackupError(f"upload failed: {type(e).__name__}") from e
    if resp.status_code != 200:
        # S3 error bodies are XML without secrets, but keep it to the status
        raise BackupError(f"upload rejected: HTTP {resp.status_code}")


def snapshot_db(db_path: str) -> bytes:
    """Consistent point-in-time copy via the SQLite backup API (WAL-safe)."""
    src = sqlite3.connect(db_path)
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            os.unlink(tmp)
    finally:
        src.close()


class Backup:
    """Periodic backup driven from the app lifespan loop: the first tick
    uploads immediately, then every config.backup_interval seconds. run()
    never raises — a failed backup is logged and retried next interval."""

    def __init__(self, config):
        self._cfg = config
        self._next = 0.0   # monotonic deadline; 0 → due right away

    @property
    def enabled(self) -> bool:
        c = self._cfg
        return bool(c.backup_s3_endpoint and c.backup_s3_bucket
                    and c.backup_s3_access_key and c.backup_s3_secret_key)

    def due(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) >= self._next

    def run(self) -> None:   # blocking: call via asyncio.to_thread
        c = self._cfg
        try:
            body = snapshot_db(c.db_path)
            weekday = _WEEKDAYS[datetime.datetime.now(datetime.timezone.utc).weekday()]
            key = f"db/gateway-{weekday}.db"
            s3_put(endpoint=c.backup_s3_endpoint, bucket=c.backup_s3_bucket,
                   region=c.backup_s3_region, access_key=c.backup_s3_access_key,
                   secret_key=c.backup_s3_secret_key, key=key, body=body,
                   url_style=c.backup_s3_url_style)
            log("backup-ok", key=key, bytes=len(body))
        except Exception as e:  # noqa: BLE001 - backups must never take the gateway down
            log_exc("backup-failed", e, error=str(e))
        finally:
            self._next = time.monotonic() + c.backup_interval
