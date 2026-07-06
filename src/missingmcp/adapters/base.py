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


def normalize_account_key(email: str) -> str:
    """Owns the spec invariant "account_key = normalized lowercased login email"
    — the cross-table join key and worker-registry key. Every adapter's
    LoginOk.account_key must come from here."""
    return email.strip().lower()


@dataclass(frozen=True)
class LoginOk:
    account_key: str   # normalized (lowercased) login identity — cross-table join key
    blob: str          # adapter-defined serialized credentials; store encrypts at rest


@dataclass(frozen=True)
class SecondFactorNeeded:
    state: object      # opaque adapter state; held in AuthState under its TTL


class WorkerForward(Protocol):
    """Forward strategy B: per-account spawned HTTP worker."""

    def command(self) -> list[str]: ...
    def env(self, port: int, workdir: str) -> dict[str, str]: ...
    def materialize(self, blob: str, workdir: str) -> None:
        """Write credential files into workdir (0600; the manager owns 0700 dirs)."""
        ...


class RemoteForward(Protocol):
    """Forward strategy A: shared remote MCP upstream, per-account credentials
    injected as request headers derived from the decrypted blob."""

    upstream_url: str

    # bytes values allowed: HTTP headers are latin-1 and httpx rejects
    # non-ASCII str values, so adapters pre-encode credential headers.
    def headers(self, blob: str) -> dict[str, "str | bytes"]: ...


def is_remote(forward: "WorkerForward | RemoteForward") -> bool:
    """Strategy dispatch (duck-typed: Protocols, no common base class)."""
    return hasattr(forward, "upstream_url")


def is_upstream_oauth(adapter) -> bool:
    """Login-shape dispatch (duck-typed, like is_remote). An upstream-OAuth
    adapter provides, instead of a credential form:
      - authorize_redirect_url(state_id: str) -> str
      - async handle_callback(query: Mapping[str, str]) -> LoginOk   (raises LoginError)
    The gateway stashes the client's OAuth params under state_id (AuthState,
    TTL 300s, one-time pop), sends the user to the provider, and finishes the
    normal verify-then-persist path when the provider calls back."""
    return hasattr(adapter, "authorize_redirect_url")


class SessionExpired(Exception):
    """A local forward's stored credentials went stale beyond repair (e.g. a
    rotated-away refresh token). The proxy surfaces the standard
    <adapter>_session_expired 502 so the client prompts a reconnect."""


class LocalForward(Protocol):
    """Forward strategy C: handled in-process — no subprocess, no shared
    upstream. Receives conn + account_key (serving a request may rotate
    upstream tokens, which must be persisted immediately) and the decrypted
    blob the proxy already fetched. Returns (status, headers, body).
    Raises SessionExpired when the credentials are beyond saving."""

    async def handle(self, conn, account_key: str, blob: str,
                     body: bytes) -> "tuple[int, dict, bytes]": ...


def is_local(forward) -> bool:
    """Strategy dispatch, beside is_remote (worker forwards have neither
    `upstream_url` nor `handle`)."""
    return hasattr(forward, "handle")


class Adapter(Protocol):
    name: str                    # registry key, log field; path prefix from spec step 3
    display_name: str            # user-facing service name in error copy
    authorize_template: str      # template filename for the credential form
    second_factor_template: str  # template filename for the second-factor form
    landing_template: str        # template filename for the connector landing page
    forward: WorkerForward | RemoteForward

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
