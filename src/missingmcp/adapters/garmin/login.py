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
from garminconnect import client as gc_client

from ...log import log_warn


class GarminLoginError(Exception):
    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        # "auth" | "blocked" | "password_reset" | "unknown"
        self.reason = reason


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


# Mobile SSO strategies 429 from datacenter fingerprints even on a clean
# dedicated IP (verified 2026-10-04 on the Hetzner box; garminconnect attributes
# it to the client fingerprint, not the IP). Running them first just burns ~10s
# and Cloudflare budget before widget/portal — the strategies that actually
# succeed through GARMIN_SSO_PROXY. Applied only when a proxy is in play.
# Requires garminconnect ≥0.3.6 (Client.login reads skip_strategies); 0.3.2
# silently ignores the setattr — see Dockerfile pin note.
_SKIP_MOBILE_WHEN_PROXIED = frozenset({"mobile+cffi", "mobile+requests"})


def supports_skip_strategies() -> bool:
    """True when the installed garminconnect honors Client.skip_strategies."""
    try:
        import inspect
        return "skip_strategies" in inspect.getsource(gc_client.Client.login)
    except (OSError, TypeError):
        return False


def _classify_login_error(exc: BaseException) -> str:
    """Map a garminconnect exception to our reason code.

    Widget titles the library doesn't recognise become ConnectionError with
    `unexpected title '…'` — classify the ones we understand so they don't
    look like IP rate-limits (blocked) and don't trip the egress breaker."""
    msg = str(exc)
    if "unexpected title 'Set Password'" in msg:
        # Garmin is forcing a password set/reset before SSO can continue.
        return "password_reset"
    if "unexpected title 'GARMIN Authentication Application'" in msg:
        # On garminconnect ≥0.3.14 this title + mfaMethod ⇒ needs_mfa. Reaching
        # us as unexpected means the sign-in page came back without MFA vars —
        # typically wrong credentials (the signin page shares that title).
        return "auth"
    if isinstance(exc, GarminConnectAuthenticationError):
        return "auth"
    if isinstance(exc, (GarminConnectTooManyRequestsError, GarminConnectConnectionError)):
        return "blocked"
    return "unknown"


def start_login(email: str, password: str, attempts: int = 2,
                backoff: float = 6.0, sleep=time.sleep,
                skip_strategies: frozenset[str] | set[str] | None = None) -> LoginResult:
    """Log in, retrying an unexpected failure once after a short backoff.

    A "blocked" outcome (429 / all strategies exhausted) is NOT retried within
    the call: one sign-in already fires several SSO requests, a second round
    doubles our footprint on an egress Cloudflare is scoring (ticket 12 — the
    egress burned on 2026-10-05 carried the most repeats), and the adapter's
    account cooldown / egress breaker decide what happens next. Wrong
    credentials ('auth') and password-reset prompts are never retried either.

    Raises GarminLoginError with .reason in {auth, blocked, password_reset, unknown}."""
    if skip_strategies and not supports_skip_strategies():
        # setattr would be a silent no-op (garminconnect 0.3.2) — surface it
        # so a future pin regression can't hide behind "mobile still runs".
        log_warn("garmin-skip-unsupported",
                 message=("garminconnect Client.login ignores skip_strategies; "
                          "mobile SSO will still run"))
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            g = Garmin(email=email, password=password, return_on_mfa=True)
            if skip_strategies:
                g.client.skip_strategies = set(skip_strategies)
            result1, result2 = g.login()
            if result1 == "needs_mfa":
                return LoginResult(status="needs_mfa", pending=(g, result2))
            return LoginResult(status="ok", tokens_json=_dump_tokens(g.client))
        except GarminConnectAuthenticationError as e:
            raise GarminLoginError(str(e), reason="auth") from e
        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as e:
            reason = _classify_login_error(e)
            if reason in ("auth", "password_reset"):
                raise GarminLoginError(str(e), reason=reason) from e
            last = e
            break
        except Exception as e:  # noqa: BLE001 - unexpected; retry once, then surface
            last = e
        if attempt + 1 < attempts:
            sleep(backoff)
    reason = _classify_login_error(last) if last else "unknown"
    raise GarminLoginError(str(last) if last else "login failed", reason=reason)


def resume_login(pending, mfa_code: str) -> str:
    client, state = pending
    client.resume_login(state, mfa_code)
    return _dump_tokens(client.client)


def verify_tokens(tokens_json: str) -> str:
    """Confirm tokens authenticate via a fresh token login; return display name.

    A successful g.login() without exception already proves authentication —
    stale/invalid tokens raise GarminConnectAuthenticationError (surfaced below).
    The name is only a log field, so an empty fullName (which a valid account may
    legitimately have) must NOT be treated as an auth failure."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "garmin_tokens.json"), "w") as f:
            f.write(tokens_json)
        try:
            g = Garmin()
            g.login(d)
            name = g.get_full_name()
        except Exception as e:  # noqa: BLE001 - surface as our error type
            raise GarminLoginError(str(e).split(":")[0].strip() or e.__class__.__name__)
    return name or ""
