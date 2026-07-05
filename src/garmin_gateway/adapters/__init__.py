from .base import (  # noqa: F401 - re-exported as the adapter API surface
    Adapter, LoginError, LoginOk, SecondFactorError, SecondFactorNeeded, WorkerForward,
)


def build_adapters(config) -> dict:
    from .garmin import GarminAdapter
    return {"garmin": GarminAdapter(config)}
