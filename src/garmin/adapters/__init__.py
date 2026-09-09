from .base import (  # noqa: F401 - re-exported as the adapter API surface
    Adapter, LoginError, LoginOk, SecondFactorError, SecondFactorNeeded, WorkerForward,
)

# Adapters we have deliberately retired. The cleanup loop fully purges any data
# still keyed to these (see store.purge_adapter and docs/adr/0001). This is an
# EXPLICIT list on purpose — "retired" must never be inferred from absence in
# build_adapters, since a temporarily unavailable adapter would then masquerade
# as a retirement and delete live data.
RETIRED_ADAPTERS: frozenset[str] = frozenset({"rohlik"})


def build_adapters(config) -> dict:
    # rohlik was a RemoteForward adapter here until 2026-07 — retired when Rohlík
    # shipped its own OAuth MCP (connect https://mcp.rohlik.cz/mcp directly).
    # The remote strategy stays first-class: see tests/test_remote_forward.py.
    from .garmin import GarminAdapter
    return {"garmin": GarminAdapter(config)}
