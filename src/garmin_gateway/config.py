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
        operator_name=env.get("OPERATOR_NAME", "the operator"),
        operator_email=env.get("OPERATOR_EMAIL", ""),
    )
