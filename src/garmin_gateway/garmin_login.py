from __future__ import annotations
import os
import tempfile
import time
from dataclasses import dataclass
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


class GarminLoginError(Exception):
    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        self.reason = reason   # "auth" (bad credentials) | "blocked" (429/403/exhausted) | "unknown"


@dataclass
class LoginResult:
    status: str                 # "ok" | "needs_mfa"
    tokens_json: str | None = None
    pending: object | None = None


def _dump_tokens(client) -> str:
    """Mirror garmin_mcp/auth_cli.py: dump to a dir, read garmin_tokens.json."""
    with tempfile.TemporaryDirectory() as d:
        client.dump(d)
        with open(os.path.join(d, "garmin_tokens.json")) as f:
            return f.read()


def start_login(email: str, password: str, attempts: int = 2,
                backoff: float = 6.0, sleep=time.sleep) -> LoginResult:
    """Log in, retrying transient/blocked failures a couple of times with a short
    backoff. Garmin rate-limits datacenter IPs (429) on mobile SSO and the widget/
    portal fallback is flaky (403); a quick retry usually gets through. Wrong
    credentials ('auth') are NOT retried. Retries are deliberately small/short so
    the synchronous authorize POST stays under the OAuth callback timeout.

    Raises GarminLoginError with .reason in {auth, blocked, unknown}."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            g = Garmin(email=email, password=password, return_on_mfa=True)
            result1, result2 = g.login()
            if result1 == "needs_mfa":
                return LoginResult(status="needs_mfa", pending=(g, result2))
            return LoginResult(status="ok", tokens_json=_dump_tokens(g.client))
        except GarminConnectAuthenticationError as e:
            raise GarminLoginError(str(e), reason="auth") from e   # bad password — never retry
        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as e:
            last = e                                               # rate-limited / blocked / flaky
        except Exception as e:  # noqa: BLE001 - unexpected; retry once, then surface
            last = e
        if attempt + 1 < attempts:
            sleep(backoff)
    reason = "blocked" if isinstance(
        last, (GarminConnectTooManyRequestsError, GarminConnectConnectionError)) else "unknown"
    raise GarminLoginError(str(last) if last else "login failed", reason=reason)


def resume_login(pending, mfa_code: str) -> str:
    client, state = pending
    client.resume_login(state, mfa_code)
    return _dump_tokens(client.client)


def verify_tokens(tokens_json: str) -> str:
    """Confirm tokens authenticate via a fresh token login; return display name."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "garmin_tokens.json"), "w") as f:
            f.write(tokens_json)
        try:
            g = Garmin()
            g.login(d)
            name = g.get_full_name()
        except Exception as e:  # noqa: BLE001 - surface as our error type
            raise GarminLoginError(str(e).split(":")[0].strip() or e.__class__.__name__)
    if not name:
        raise GarminLoginError("session is not authenticated (no profile returned)")
    return name
