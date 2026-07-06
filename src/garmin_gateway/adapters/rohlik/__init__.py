"""Rohlik adapter — remote-forward strategy A against the hosted Rohlik MCP.

Deliberate asymmetry vs Garmin (spec 2026-07-05): Rohlik's upstream MCP
authenticates every request with rhl-email/rhl-pass headers, so the blob must
keep the email AND password. They live only in the encrypted blob — never
logged, never written anywhere else.
"""
from __future__ import annotations
import json
import re
from typing import Mapping
import httpx
from ..base import LoginError, LoginOk, SecondFactorNeeded, normalize_account_key

# Same rules as the TS proxy's security.ts (validateEmail / validatePassword).
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_EMAIL_MAX = 254
_PASSWORD_MAX = 1000

_MSG_BAD_CREDS = "Rohlík sign-in failed — check your Rohlík email and password."
_MSG_UNREACHABLE = "Rohlík could not be reached, please try again."

# Cheapest valid MCP call that exercises the upstream's header auth without
# touching account data.
_INITIALIZE = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "missingmcp-gateway", "version": "1.0"}},
}).encode()


class RohlikRemoteForward:
    """RemoteForward strategy for the shared hosted Rohlik MCP: per-account
    credentials injected as the rhl-* headers it expects on every request."""

    def __init__(self, config):
        self.upstream_url = config.rohlik_mcp_url

    def headers(self, blob: str) -> dict:
        d = json.loads(blob)
        # latin-1 = HTTP's header charset and exactly what the TS proxy (fetch
        # ByteString) sent; httpx rejects non-ASCII str values, so pre-encode.
        # start_login guarantees persisted blobs are latin-1-encodable.
        return {"rhl-email": d["email"].encode("latin-1"),
                "rhl-pass": d["password"].encode("latin-1")}


class RohlikAdapter:
    name = "rohlik"
    display_name = "Rohlík"
    authorize_template = "rohlik_authorize.html"
    second_factor_template = ""  # no second factor: start_login never defers
    landing_template = "rohlik.html"

    def __init__(self, config):
        self.forward = RohlikRemoteForward(config)

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("rohlik_email", "")

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        email = form.get("rohlik_email", "").strip()
        password = form.get("rohlik_password", "")
        if not email or len(email) > _EMAIL_MAX or not _EMAIL_RE.match(email):
            raise LoginError("Please enter a valid email address.", reason="auth")
        if not password or len(password) > _PASSWORD_MAX:
            raise LoginError("Please enter your password.", reason="auth")
        try:
            email.encode("latin-1")
            password.encode("latin-1")
        except UnicodeEncodeError:
            # can never be carried in the rhl-* headers (see RohlikRemoteForward)
            raise LoginError("Rohlík sign-in failed — your email or password contains "
                             "characters that cannot be sent to Rohlík.", reason="auth") from None
        # No login call here: the upstream is sessionless — credentials are checked
        # by verify() below and then re-sent with every proxied request, so (unlike
        # Garmin) the password itself goes into the blob; the store encrypts it.
        return LoginOk(account_key=normalize_account_key(email),
                       blob=json.dumps({"email": email, "password": password}))

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        # unreachable: start_login never returns SecondFactorNeeded
        raise LoginError("Rohlík sign-in does not use a verification code")

    def verify(self, blob: str) -> str:
        # The step the TS proxy lacked: prove the credentials against the upstream
        # before they are persisted. stream() decides on the status line alone —
        # the upstream may answer with a long-lived SSE body we must never read
        # (or let into exceptions/logs).
        headers = {**self.forward.headers(blob),
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        try:
            with httpx.stream("POST", self.forward.upstream_url, headers=headers,
                              content=_INITIALIZE, timeout=15.0) as resp:
                status = resp.status_code
        except httpx.HTTPError as e:
            raise LoginError(_MSG_UNREACHABLE) from e
        if status in (401, 403):
            raise LoginError(_MSG_BAD_CREDS, reason="auth")
        if not 200 <= status < 300:
            raise LoginError(_MSG_UNREACHABLE)
        return json.loads(blob)["email"]
