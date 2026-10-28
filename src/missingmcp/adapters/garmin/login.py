from __future__ import annotations
import os
import re
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
    def __init__(self, message: str, reason: str = "unknown", page: str | None = None):
        super().__init__(message)
        # "auth" (bad credentials) | "password_reset" (Garmin demands a new
        # password) | "blocked" (429/403/exhausted) | "egress" (our proxy
        # couldn't carry the request — Garmin never saw it) | "unknown"
        self.reason = reason
        self.page = page       # title of the SSO page Garmin answered with, if any


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


# Through GARMIN_SSO_PROXY only the widget strategy ever succeeds. Mobile 429s
# from datacenter fingerprints even on a clean dedicated IP (garminconnect
# attributes it to the client fingerprint, not the IP; 390 of 390 in the 30 h
# before 2026-10-12), and portal hits a Cloudflare bot challenge (403, 80 of
# 80) after 10-20 s anti-WAF waits each — pushing a failed sign-in past
# login_timeout and only feeding Cloudflare more failures from our egress.
# Applied only when a proxy is in play. garminconnect 0.3.20 fixed the mobile
# strategy (it now loads the sign-in page before posting, #479); whether that
# gets it through our proxies is untried — mobile stays skipped until tested.
_SKIP_WHEN_PROXIED = frozenset({"mobile+cffi", "mobile+requests",
                                "portal+cffi", "portal+requests"})

# garminconnect recognizes wrong credentials only by keywords in the widget
# page's title, but Garmin re-renders the sign-in page itself ("GARMIN
# Authentication Application") with the error in its body — the library then
# reports an "unexpected title" connection error, and we used to call that
# "blocked". 38 of 40 "blocked" sign-ins in the 30 h before 2026-10-12 were
# this page, none a 429. MFA pages carry the same title but are recognized by
# their mfaMethod vars first (garminconnect >= 0.3.17), so what's left is the
# sign-in form coming back: Garmin didn't accept the email/password.
_UNEXPECTED_TITLE = re.compile(r"unexpected title '([^']{0,80})'")
_PAGE_REASONS = {
    "GARMIN Authentication Application": "auth",
    "Set Password": "password_reset",
}


# A proxy that can't carry the sign-in fails before Garmin sees a byte: curl
# reports an unreachable proxy as (5)/(7)/(97) and a refused CONNECT (squid
# without its outgoing address: 503, wrong credentials: 407) as "CONNECT tunnel
# failed" — named ConnectionError or ProxyError depending on the curl_cffi
# build. 2026-10-16..18: three proxy ports lost their floating IPs and every
# sign-in through them read "Garmin is rate-limiting" while Garmin let the
# same accounts in through the fourth port (prod: "curl: (7) CONNECT tunnel
# failed, response 503"). Only the widget+cffi strategy runs behind a proxy,
# so its curl text is the last error.
_EGRESS_FAILURE = re.compile(r"CONNECT tunnel failed|ProxyError|curl: \((?:5|7|97)\)")


def _page_title(err: Exception) -> str | None:
    m = _UNEXPECTED_TITLE.search(str(err))
    return m.group(1) if m else None


def start_login(email: str, password: str, attempts: int = 2,
                backoff: float = 6.0, sleep=time.sleep,
                skip_strategies: frozenset[str] | set[str] | None = None) -> LoginResult:
    """Log in, retrying an unexpected failure once after a short backoff.

    A "blocked" outcome (429 / all strategies exhausted) is NOT retried within
    the call: one sign-in already fires several SSO requests, a second round
    doubles our footprint on an egress Cloudflare is scoring (ticket 12 — the
    egress burned on 2026-10-05 carried the most repeats), and the adapter's
    account cooldown / egress breaker decide what happens next. Wrong
    credentials ('auth') are never retried either.

    Raises GarminLoginError with .reason in {auth, password_reset, blocked,
    egress, unknown}; .page carries the SSO page title when Garmin answered with a page
    the library didn't expect."""
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
            raise GarminLoginError(str(e), reason="auth") from e   # bad password — never retry
        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as e:
            last = e                                               # rate-limited / blocked
            break
        except Exception as e:  # noqa: BLE001 - unexpected; retry once, then surface
            last = e
        if attempt + 1 < attempts:
            sleep(backoff)
    reason = "blocked" if isinstance(
        last, (GarminConnectTooManyRequestsError, GarminConnectConnectionError)) else "unknown"
    if reason == "blocked" and _EGRESS_FAILURE.search(str(last)):
        reason = "egress"
    page = _page_title(last) if last else None
    reason = _PAGE_REASONS.get(page, reason)
    raise GarminLoginError(str(last) if last else "login failed", reason=reason, page=page)


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
