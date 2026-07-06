from .base import (  # noqa: F401 - re-exported as the adapter API surface
    Adapter, LoginError, LoginOk, SecondFactorError, SecondFactorNeeded, WorkerForward,
)


def build_adapters(config) -> dict:
    # rohlik was a RemoteForward adapter here until 2026-07 — retired when Rohlík
    # shipped its own OAuth MCP (connect https://mcp.rohlik.cz/mcp directly).
    # The remote strategy stays first-class: see tests/test_remote_forward.py.
    from .garmin import GarminAdapter
    adapters = {"garmin": GarminAdapter(config)}
    # whoop needs an operator-registered WHOOP app; without credentials the
    # connector stays off (local dev, CI) — same pattern as BACKUP_S3_*.
    if config.whoop_client_id and config.whoop_client_secret:
        from .whoop import WhoopAdapter
        adapters["whoop"] = WhoopAdapter(config)
    return adapters
