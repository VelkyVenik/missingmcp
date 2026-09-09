from __future__ import annotations
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Callable

_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# The shape of a client_id this server issues: new_secret(16) is
# secrets.token_urlsafe(16) -> 22 chars of [A-Za-z0-9_-]. Ranged, not fixed at 22,
# so a future nbytes change doesn't silently reclassify every real registration.
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def security_headers(script_hosts: tuple[str, ...] = (),
                     connect_hosts: tuple[str, ...] = ()) -> dict[str, str]:
    # NOTE: no `form-action` directive. This is an OAuth authorization server:
    # the login/MFA form POSTs to /oauth/authorize and the server replies with a
    # 302 to the client's registered redirect_uri (an arbitrary origin/port, e.g.
    # http://localhost:3118/callback for the Claude Code CLI). `form-action 'self'`
    # makes the browser block that cross-origin redirect after the form POST, so
    # the auth code never reaches the client's callback. Redirect safety is
    # enforced by validate_redirect_uri() (exact match against the DCR-registered
    # URIs), not by CSP.
    # script_hosts/connect_hosts widen script-src/connect-src beyond 'self' —
    # used only for the PostHog assets CDN + ingestion host when telemetry is
    # enabled (both directives otherwise fall back to default-src 'self').
    csp = "default-src 'self'; style-src 'self' 'unsafe-inline'"
    if script_hosts:
        csp += "; script-src 'self' " + " ".join(script_hosts)
    if connect_hosts:
        csp += "; connect-src 'self' " + " ".join(connect_hosts)
    return {
        "Content-Security-Policy": csp,
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method != "S256" or not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(expected, code_challenge)


def validate_redirect_uri(uri: str, allowed: list[str]) -> bool:
    return uri in allowed


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def validate_session_id(sid: str) -> bool:
    return bool(_SESSION_RE.match(sid))


def looks_like_issued_client_id(client_id: str) -> bool:
    """True when `client_id` could be one this server issued — i.e. the shape of
    `new_secret(16)` (`secrets.token_urlsafe`, 22 chars of [A-Za-z0-9_-]).

    Not a security check: an unknown client is rejected either way. It only tells
    the two reasons apart, because they need opposite advice. A value carrying an
    `@` or a `.` cannot have come from us, and in practice is the user's own email
    typed into the client's optional "OAuth Client ID" field — for which
    re-adding the connector is useless advice."""
    return bool(_CLIENT_ID_RE.match(client_id))


def valid_email(email: str) -> bool:
    """Cheap syntactic check for opt-in forms — no deliverability probe, no DNS.
    Rejects empty, over-long (>254, the RFC 5321 max), and anything without a
    plausible local@domain.tld shape."""
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float) -> bool:
        now = self._clock()
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def gc(self, max_idle: float = 3600.0) -> None:
        """Drop keys with no recent hits. Without this, _hits grows one entry per
        distinct key forever — e.g. proxy.authenticate rate-limits on the (unverified)
        token hash, so rotating Bearer values would leak memory. Call periodically."""
        now = self._clock()
        for key in list(self._hits):
            q = self._hits[key]
            if not q or q[-1] <= now - max_idle:
                del self._hits[key]


class CsrfStore:
    def __init__(self, ttl: float = 600, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._tokens: dict[str, float] = {}

    def issue(self) -> str:
        self._gc()
        tok = secrets.token_urlsafe(24)
        self._tokens[tok] = self._clock()
        return tok

    def consume(self, token: str) -> bool:
        self._gc()
        return self._tokens.pop(token, None) is not None

    def _gc(self) -> None:
        now = self._clock()
        for t, ts in list(self._tokens.items()):
            if now - ts > self._ttl:
                self._tokens.pop(t, None)


async def read_body_limited(request, max_bytes: int = 1_048_576) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
