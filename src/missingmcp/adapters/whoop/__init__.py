from __future__ import annotations
import json
from typing import Mapping
import httpx
from ..base import LoginError, LoginOk, normalize_account_key
from .api import HTTP_TIMEOUT_S, WhoopAuthError
from .mcp import WhoopLocalForward


class WhoopAdapter:
    """Upstream-OAuth login (shape C) + local forward (strategy C): WHOOP is a
    real OAuth provider, so users sign in at WHOOP — this gateway never sees a
    WHOOP password — and the MCP server runs in-process (mcp.py)."""

    name = "whoop"
    display_name = "WHOOP"
    authorize_template = ""        # no credential form: login happens at WHOOP
    second_factor_template = ""
    landing_template = "whoop.html"

    def __init__(self, config):
        self.forward = WhoopLocalForward(config)
        self.api = self.forward.api

    # --- upstream-OAuth login shape ------------------------------------------
    def authorize_redirect_url(self, state_id: str) -> str:
        return self.api.auth_url(state_id)

    async def handle_callback(self, query: Mapping[str, str]) -> LoginOk:
        try:
            blob = await self.api.exchange_code(query.get("code", ""))
            profile = await self.api.fetch_profile(blob["access_token"])
        except (WhoopAuthError, httpx.HTTPError) as e:
            raise LoginError("WHOOP sign-in could not be completed — please try again.") from e
        email = profile.get("email", "")
        if not email:
            raise LoginError("WHOOP did not return an account email.")
        blob["user_id"] = profile.get("user_id")
        blob["email"] = email
        return LoginOk(account_key=normalize_account_key(email), blob=json.dumps(blob))

    # --- form-login contract stubs (unreachable: app.py registers no authorize
    # POST for upstream-OAuth adapters) ----------------------------------------
    def login_hint(self, form: Mapping[str, str]) -> str:
        return ""

    def start_login(self, form: Mapping[str, str]) -> LoginOk:
        raise LoginError("WHOOP sign-in happens at WHOOP, not here.")

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        raise LoginError("WHOOP sign-in happens at WHOOP, not here.")

    def verify(self, blob: str) -> str:
        """Gate for verify-then-persist: re-fetch the profile with the blob's
        access token (sync, one-off at login time — garmin's login is equally
        blocking) and return a display name for the logs."""
        d = json.loads(blob)
        try:
            r = httpx.get(self.api.profile_url,
                          headers={"Authorization": f"Bearer {d['access_token']}"},
                          timeout=HTTP_TIMEOUT_S)
        except httpx.HTTPError as e:
            raise LoginError("WHOOP could not be reached to verify the sign-in.") from e
        if r.status_code != 200:
            raise LoginError("WHOOP sign-in could not be verified.")
        p = r.json()
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
