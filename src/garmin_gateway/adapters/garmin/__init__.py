from __future__ import annotations
import os


class GarminWorkerForward:
    """WorkerForward strategy for the unmodified garmin-mcp worker: its documented
    CLI + env contract (GARMIN_MCP_* / GARMINTOKENS) and token-file materialization."""

    def __init__(self, config):
        self._cfg = config

    def command(self) -> list[str]:
        return self._cfg.garmin_mcp_cmd

    def env(self, port: int, workdir: str) -> dict[str, str]:
        return {
            "GARMIN_MCP_TRANSPORT": "streamable-http",
            "GARMIN_MCP_HOST": "127.0.0.1",
            "GARMIN_MCP_PORT": str(port),
            "GARMINTOKENS": workdir,
        }

    def materialize(self, blob: str, workdir: str) -> None:
        path = os.path.join(workdir, "garmin_tokens.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(blob)
