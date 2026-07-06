from .base import (  # noqa: F401 - re-exported as the adapter API surface
    Adapter, LoginError, LoginOk, SecondFactorError, SecondFactorNeeded, WorkerForward,
)


def build_adapters(config) -> dict:
    from .garmin import GarminAdapter
    from .rohlik import RohlikAdapter
    return {"garmin": GarminAdapter(config), "rohlik": RohlikAdapter(config)}
