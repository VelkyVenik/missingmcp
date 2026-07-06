from __future__ import annotations
import os
from typing import Mapping
from ..base import (LoginError, LoginOk, SecondFactorError, SecondFactorNeeded,
                    normalize_account_key)
from . import login


class GarminWorkerForward:
    """WorkerForward strategy for the unmodified garmin-mcp worker: its documented
    CLI + env contract (GARMIN_MCP_* / GARMINTOKENS) and token-file materialization."""

    def __init__(self, config):
        self._cfg = config

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

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("garmin_email", "")

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        email = form.get("garmin_email", "")
        password = form.get("garmin_password", "")
        try:
            result = login.start_login(email, password)
        except login.GarminLoginError as e:
            reason = getattr(e, "reason", "unknown")
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
