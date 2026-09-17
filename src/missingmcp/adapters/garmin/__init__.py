from __future__ import annotations
import json
import os
import threading
import time
from typing import Mapping
from ...log import log
from ..base import (LoginError, LoginOk, SecondFactorError, SecondFactorNeeded,
                    normalize_account_key)
from . import login


# The worker's two possible sign-in verdicts, printed exactly once per worker
# life by garmin_mcp's login path. Since the login moved to a background thread
# (garmin_mcp #255, pinned from e8554bc) these lines are the ONLY startup signal
# that the stored tokens still work — the worker answers /healthz either way.
# Substring match, not equality: the worker appends detail after each.
_LOGIN_OK_LINE = "Garmin Connect client initialized successfully"
_LOGIN_FAILED_LINES = (
    "Garmin Connect client failed to initialize",       # >= e8554bc (background login)
    "Failed to initialize Garmin Connect client",       # older pins (exit-on-failure era)
)


class GarminWorkerForward:
    """WorkerForward strategy for the unmodified garmin-mcp worker: its documented
    CLI + env contract (GARMIN_MCP_* / GARMINTOKENS) and token-file materialization."""

    def __init__(self, config):
        self._cfg = config

    def login_outcome(self, line: str) -> str | None:
        """Classify one worker log line as the sign-in outcome — "ok", "failed",
        or None (not a sign-in line). Fed by the worker output pump into the
        spawn's LoginGate; ensure_worker blocks on it so stale tokens still
        become a re-auth 401 instead of per-call "run garmin-mcp-auth" tool
        errors that a missingmcp user can't act on."""
        if _LOGIN_OK_LINE in line:
            return "ok"
        if any(marker in line for marker in _LOGIN_FAILED_LINES):
            return "failed"
        return None

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

    def read_back(self, workdir: str) -> str | None:
        # garth (inside the worker) rewrites this file when Garmin rotates the
        # refresh token, and it may not write atomically — a torn file must
        # never reach the store, so anything unparseable reads as "nothing".
        path = os.path.join(workdir, "garmin_tokens.json")
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            json.loads(content)
        except (OSError, ValueError):
            return None
        return content


# When Garmin's Cloudflare rate-limits our egress IP (reliability ticket 12),
# every sign-in attempt burns ~30s cycling login strategies before failing
# "blocked" — and those very attempts keep the rate limiter hot. Once one
# attempt reports blocked, fail new sign-ins fast for a cooldown instead: the
# user gets the honest "wait a few minutes" answer immediately, and our SSO
# footprint drops to zero so the block can decay.
_SSO_BREAKER_COOLDOWN_S = 300.0


class SsoBreaker:
    """Process-local circuit breaker for the Garmin SSO portal sign-in.

    Time-based and deliberately tiny: `trip()` opens it for `cooldown` seconds,
    `remaining()` says how long it stays open; expiry alone closes it (the next
    attempt after the cooldown is the half-open probe). Thread-safe because
    `start_login` runs on `asyncio.to_thread` workers — including *abandoned*
    ones whose authorize POST already timed out: when such a thread finally
    fails "blocked", its trip() is fresh evidence the portal is still blocking,
    arriving exactly when the next request needs it. Process-local state is
    fine here for the same reason it is for `RateLimiter` (single-node)."""

    def __init__(self, cooldown: float = _SSO_BREAKER_COOLDOWN_S, clock=time.monotonic):
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._open_until = 0.0

    def trip(self) -> None:
        with self._lock:
            self._open_until = self._clock() + self.cooldown

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - self._clock())


def _login_error_message(reason: str) -> str:
    if reason == "blocked":
        # Garmin (via Cloudflare) rate-limits fresh logins on the mobile SSO
        # endpoint — per-account, not per-IP (garth#217, garminconnect#344) — and
        # the widget/portal fallback can flake. Not the user's fault; a retry usually works.
        return ("Garmin is temporarily rate-limiting new sign-ins (a limit on "
                "Garmin's side, not your password). Please wait a couple of minutes and try again.")
    if reason == "auth":
        return "Garmin sign-in failed — check your Garmin email and password."
    return "Garmin sign-in failed, please try again."


class GarminAdapter:
    name = "garmin"
    display_name = "Garmin"
    authorize_template = "authorize.html"
    second_factor_template = "mfa.html"
    landing_template = "garmin.html"

    def __init__(self, config):
        self.forward = GarminWorkerForward(config)
        self.breaker = SsoBreaker()

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("garmin_email", "")

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        # One breaker reference for the whole call: an abandoned (timed-out)
        # thread must trip the breaker that was active when its attempt began,
        # never a replacement installed later (tests swap breakers per test).
        breaker = self.breaker
        remaining = breaker.remaining()
        if remaining > 0:
            # Fail fast while the SSO portal is rate-limiting us: same message
            # and reason as a live "blocked" failure, so the form copy and the
            # triage classification stay identical — minus the 30s of doomed
            # strategies each attempt would otherwise fire at the limiter.
            log("login-breaker-reject", remaining_s=int(remaining))
            raise LoginError(_login_error_message("blocked"), reason="blocked")
        email = form.get("garmin_email", "")
        password = form.get("garmin_password", "")
        try:
            result = login.start_login(email, password)
        except login.GarminLoginError as e:
            reason = getattr(e, "reason", "unknown")
            if reason == "blocked":
                breaker.trip()
                log("login-breaker-open", cooldown_s=int(breaker.cooldown))
            raise LoginError(_login_error_message(reason), reason=reason) from e
        finally:
            del password  # never retained beyond the login call
        if result.status == "needs_mfa":
            return SecondFactorNeeded(state=(result.pending, email))
        return LoginOk(account_key=normalize_account_key(email), blob=result.tokens_json)

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        pending, email = state
        try:
            tokens = login.resume_login(pending, form.get("mfa_code", ""))
        except Exception as e:  # noqa: BLE001 - wrong/expired code: caller re-prompts
            raise SecondFactorError("Incorrect or expired code, try again", state=state) from e
        return LoginOk(account_key=normalize_account_key(email), blob=tokens)

    def verify(self, blob: str) -> str:
        try:
            return login.verify_tokens(blob)
        except login.GarminLoginError as e:
            raise LoginError("Garmin sign-in could not be verified") from e
