from __future__ import annotations
import json
import os
from typing import Mapping
from ...log import log
from ..base import (LoginError, LoginOk, SecondFactorError, SecondFactorNeeded,
                    normalize_account_key)
from . import egress, login
from .egress import EgressPool, SsoBreaker  # noqa: F401 - SsoBreaker re-exported
from .login import _SKIP_MOBILE_WHEN_PROXIED


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


def _attempt_message(egress_label: str, outcome: str) -> str:
    """Railway's log UI indexes/displays `message`; without it, structured
    events like garmin-login-attempt are invisible in text search (ops saw
    only garminconnect's bridged 'Login failed:…' lines on 2026-10-05)."""
    return f"garmin login via {egress_label}: {outcome}"


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
        self.pool = EgressPool(config.garmin_sso_proxy)
        if self.pool.proxied:
            labels = ",".join(r.label for r in self.pool.routes)
            log("garmin-sso-proxy", egress=labels,
                message=f"garmin SSO egress routes: {labels}")

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("garmin_email", "")

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        email = form.get("garmin_email", "")
        account = normalize_account_key(email)
        remaining = self.pool.account_remaining(account)
        if remaining > 0:
            # Garmin is limiting this very account (any IP would do the same):
            # answer fast instead of spending ~30s of strategies on it.
            log("login-breaker-reject", remaining_s=int(remaining), scope="account",
                account=account,
                message=f"garmin login rejected (account cooldown {int(remaining)}s)")
            raise LoginError(_login_error_message("blocked"), reason="blocked")
        # One route (and so one breaker) for the whole call: an abandoned
        # (timed-out) thread must trip the breaker of the egress its attempt
        # actually used, never whatever is preferred by the time it finishes.
        route = self.pool.pick(account)
        if route is None:
            # Every egress is cooling down: fail fast with the same message and
            # reason as a live "blocked" failure, so the form copy and the
            # triage classification stay identical — minus the 30s of doomed
            # strategies each attempt would otherwise fire at the limiter.
            wait = int(self.pool.min_remaining())
            log("login-breaker-reject", remaining_s=wait, scope="egress",
                account=account,
                message=f"garmin login rejected (all egresses cooling {wait}s)")
            raise LoginError(_login_error_message("blocked"), reason="blocked")
        password = form.get("garmin_password", "")
        skip = _SKIP_MOBILE_WHEN_PROXIED if route.proxy else None
        try:
            with egress.via(route.proxy):
                result = login.start_login(email, password, skip_strategies=skip)
        except login.GarminLoginError as e:
            reason = getattr(e, "reason", "unknown")
            log("garmin-login-attempt", account=account, egress=route.label,
                outcome=reason, message=_attempt_message(route.label, reason))
            if reason == "blocked":
                # The account always cools down; the egress only once several
                # accounts are blocked on it (an IP-level limit) — then its
                # accounts move to their next route.
                scope = self.pool.record_blocked(route, account)
                cooldown = (route.breaker.cooldown if scope == "egress"
                            else egress.ACCOUNT_COOLDOWN_S)
                log("login-breaker-open", cooldown_s=int(cooldown), scope=scope,
                    egress=route.label, account=account,
                    message=(f"garmin {scope} breaker open on {route.label} "
                             f"for {int(cooldown)}s"))
            raise LoginError(_login_error_message(reason), reason=reason) from e
        finally:
            del password  # never retained beyond the login call
        self.pool.record_ok(route)
        log("garmin-login-attempt", account=account, egress=route.label,
            outcome=result.status,
            message=_attempt_message(route.label, result.status))
        if result.status == "needs_mfa":
            return SecondFactorNeeded(state=(result.pending, email, route.proxy))
        return LoginOk(account_key=account, blob=result.tokens_json)

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        # The MFA step must leave through the egress the sign-in started on
        # (same Garmin session); it bypasses the breakers deliberately — a
        # pending session already passed the portal.
        pending, email, *rest = state
        proxy = rest[0] if rest else None
        try:
            with egress.via(proxy):
                tokens = login.resume_login(pending, form.get("mfa_code", ""))
        except Exception as e:  # noqa: BLE001 - wrong/expired code: caller re-prompts
            raise SecondFactorError("Incorrect or expired code, try again", state=state) from e
        return LoginOk(account_key=normalize_account_key(email), blob=tokens)

    def verify(self, blob: str) -> str:
        # Token verify talks to Garmin's API/token hosts, not the SSO portal,
        # but rides the same egress preference as sign-in for consistency.
        route = self.pool.pick("") or self.pool.routes[0]
        try:
            with egress.via(route.proxy):
                return login.verify_tokens(blob)
        except login.GarminLoginError as e:
            raise LoginError("Garmin sign-in could not be verified") from e
