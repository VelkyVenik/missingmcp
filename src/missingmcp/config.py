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
    access_token_ttl: int         # seconds; 0 disables expiry
    orphan_client_ttl: int        # seconds; a 0-token OAuth client older than this is swept
    login_timeout: int            # seconds; wall-clock cap on a synchronous adapter sign-in
    operator_name: str
    operator_email: str
    operator_url: str             # optional homepage the operator name links to
    # Off-box DB backups (backup.py); disabled when the S3 credentials are unset.
    backup_s3_endpoint: str
    backup_s3_bucket: str
    backup_s3_access_key: str
    backup_s3_secret_key: str
    backup_s3_region: str
    backup_s3_url_style: str      # "virtual-host" (Railway buckets) | "path"
    backup_interval: int          # seconds
    # WHOOP adapter (adapters/whoop). The adapter is registered only when both
    # client credentials are set — see adapters.build_adapters.
    whoop_client_id: str
    whoop_client_secret: str
    whoop_api_base: str           # tests/staging override; both OAuth and data URLs derive from it


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    secret = env.get("GATEWAY_SECRET", "")
    if len(secret) < 32:
        raise ValueError("GATEWAY_SECRET must be set and at least 32 characters")
    if secret.startswith("change-me"):
        raise ValueError(
            "GATEWAY_SECRET is still the example placeholder; set a real random "
            "secret (e.g. `openssl rand -base64 48`)"
        )
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
        access_token_ttl=int(env.get("ACCESS_TOKEN_TTL_DAYS", "90")) * 86400,
        # Deliberately a hardcoded constant, not env-configurable (docs/adr/0001).
        # 30 DAYS, not hours: Claude/ChatGPT cache their DCR client per org and
        # re-use it for later (re-)authorizations — sweeping a cached client
        # strands the user on "unknown client_id" until they remove + re-add
        # the connector. The sweep only bounds scanner-registration growth.
        orphan_client_ttl=30 * 86400,
        # Hardcoded wall-clock cap on a blocking adapter sign-in (garminconnect does
        # synchronous network I/O). Long enough for a normal login + one transient
        # retry, short enough that a rate-limited Garmin can't hang the request for
        # minutes (observed: a 125s authorize POST before the client gave up).
        login_timeout=30,
        operator_name=env.get("OPERATOR_NAME", "the operator"),
        operator_email=env.get("OPERATOR_EMAIL", ""),
        operator_url=env.get("OPERATOR_URL", "").strip(),
        backup_s3_endpoint=env.get("BACKUP_S3_ENDPOINT", "").rstrip("/"),
        backup_s3_bucket=env.get("BACKUP_S3_BUCKET", ""),
        backup_s3_access_key=env.get("BACKUP_S3_ACCESS_KEY", ""),
        backup_s3_secret_key=env.get("BACKUP_S3_SECRET_KEY", ""),
        backup_s3_region=env.get("BACKUP_S3_REGION", "auto"),
        backup_s3_url_style=env.get("BACKUP_S3_URL_STYLE", "virtual-host"),
        backup_interval=int(env.get("BACKUP_INTERVAL_HOURS", "6")) * 3600,
        whoop_client_id=env.get("WHOOP_CLIENT_ID", ""),
        whoop_client_secret=env.get("WHOOP_CLIENT_SECRET", ""),
        whoop_api_base=env.get("WHOOP_API_BASE", "https://api.prod.whoop.com").rstrip("/"),
    )
