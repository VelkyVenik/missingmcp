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

# Cheapest MCP call that exercises the actual Rohlik login. It must be a
# tools/call: the upstream logs in lazily, so initialize succeeds with ANY
# credentials, and even a failed login answers HTTP 200 with the 401 buried in
# a result.isError text (both confirmed against rohlik_mcp 2.14.7, 2026-07-06).
# The upstream is stateless — tools/call works without a prior initialize.
_VERIFY_CALL = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "get_user_info", "arguments": {}},
}).encode()

_VERIFY_BODY_CAP = 65536  # the get_user_info result is small; don't read more


class _VerifyUnreadable(Exception):
    """The 2xx verify response couldn't be understood — fail closed."""


def _tool_error_text(body: bytes) -> "str | None":
    """None when the verify tools/call succeeded; the upstream's error text when
    it reported a failure. Accepts both response shapes (SSE `data:` line or
    plain JSON). Raises _VerifyUnreadable when the body fits neither — verify-
    then-persist means an unconfirmable login must not be persisted."""
    text = body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:]
            break
    try:
        msg = json.loads(text)
    except ValueError:
        raise _VerifyUnreadable() from None
    if not isinstance(msg, dict):
        raise _VerifyUnreadable()
    if "error" in msg:  # JSON-RPC-level failure (bad request/unknown tool)
        return str(msg["error"])
    result = msg.get("result")
    if not isinstance(result, dict):
        raise _VerifyUnreadable()
    if not result.get("isError"):
        return None
    parts = result.get("content") or []
    return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))


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
        # before they are persisted, by forcing a real Rohlik login (_VERIFY_CALL).
        # The body must be read (capped): the login failure hides in a 200.
        headers = {**self.forward.headers(blob),
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        try:
            with httpx.stream("POST", self.forward.upstream_url, headers=headers,
                              content=_VERIFY_CALL, timeout=15.0) as resp:
                status = resp.status_code
                body = b""
                if 200 <= status < 300:
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > _VERIFY_BODY_CAP:
                            break
        except httpx.HTTPError as e:
            raise LoginError(_MSG_UNREACHABLE) from e
        if status in (401, 403):
            raise LoginError(_MSG_BAD_CREDS, reason="auth")
        if not 200 <= status < 300:
            raise LoginError(_MSG_UNREACHABLE)
        try:
            error_text = _tool_error_text(body)
        except _VerifyUnreadable:
            raise LoginError(_MSG_UNREACHABLE) from None
        if error_text is None:
            return json.loads(blob)["email"]
        # error_text echoes the account email — never let it into the
        # exception (user-facing + logged); decide on it, then drop it.
        if "401" in error_text or "403" in error_text or "Unauthorized" in error_text:
            raise LoginError(_MSG_BAD_CREDS, reason="auth")
        raise LoginError(_MSG_UNREACHABLE)
