"""Adapter contract — the seam between the gateway core and upstream services.

The core (oauth.py flow, WorkerManager, proxy) owns: form rendering, CSRF,
rate limits, OAuth params, second-factor state TTL, encryption of the blob,
code mint + redirect. An adapter owns: what the credential fields are, how
login works, what the blob contains, and how to reach the upstream.

Spec: docs/superpowers/specs/2026-07-05-multi-adapter-gateway-design.md.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Protocol


class LoginError(Exception):
    """Login/verify failed. str(e) is user-facing (re-rendered on the form);
    .reason feeds structured logs: "auth" | "blocked" | "unknown"."""

    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        self.reason = reason


class SecondFactorError(Exception):
    """Second-factor code rejected. Carries the (possibly refreshed) opaque
    state so the flow can re-stash it and re-prompt with str(e)."""

    def __init__(self, message: str, state: object):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class LoginOk:
    account_key: str   # normalized (lowercased) login identity — cross-table join key
    blob: str          # adapter-defined serialized credentials; store encrypts at rest


@dataclass(frozen=True)
class SecondFactorNeeded:
    state: object      # opaque adapter state; held in AuthState under its TTL


class WorkerForward(Protocol):
    """Forward strategy B: per-account spawned HTTP worker.
    (Strategy A, RemoteForward, arrives with the rohlik adapter — spec step 4.)"""

    def command(self) -> list[str]: ...
    def env(self, port: int, workdir: str) -> dict[str, str]: ...
    def materialize(self, blob: str, workdir: str) -> None:
        """Write credential files into workdir (0600; the manager owns 0700 dirs)."""
        ...


class Adapter(Protocol):
    name: str                    # registry key, log field; path prefix from spec step 3
    display_name: str            # user-facing service name in error copy
    authorize_template: str      # template filename for the credential form
    second_factor_template: str  # template filename for the second-factor form
    forward: WorkerForward

    def login_hint(self, form: Mapping[str, str]) -> str:
        """The login identity as typed (for the login-start log line)."""
        ...

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        """Attempt login from the adapter's own form fields. Raises LoginError.
        Must not retain secrets beyond the call."""
        ...

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        """Complete a SecondFactorNeeded login. Raises SecondFactorError (retryable)
        or LoginError (start over)."""
        ...

    def verify(self, blob: str) -> str:
        """Confirm the blob authenticates against the upstream; return a display
        name for logging. Raises LoginError. Gates persistence on every path."""
        ...
