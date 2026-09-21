from __future__ import annotations
import time
from ...log import log

# The page garminconnect's login strategies hit first; its status from the
# gateway's egress IP is the signal being graphed (reliability ticket 12).
_SSO_PAGE = "https://sso.garmin.com/portal/sso/en-US/sign-in"
_MIN_INTERVAL_S = 60           # floor a mistyped env var — never hammer Garmin
_FETCH_TIMEOUT_S = 15


class SsoProbe:
    """Periodic, attempt-independent reachability probe of Garmin's SSO page.

    Ticket 12: a sign-in block is only observable when a user happens to
    attempt one, so the block timeline is confounded by our own traffic. One
    browser-impersonated GET per interval — the same curl_cffi/chrome
    fingerprint the real sign-in presents — gives an independent timeline:
    blocks during hours with no sign-in attempts point at IP reputation;
    blocks tracking our own bursts point at our multi-account signature.

    Env-gated by SSO_PROBE_INTERVAL (seconds; 0 = off, the default). Emits
    `sso-probe` with the HTTP status (`error` carries the exception type when
    the request itself failed). Follows backup.Backup's enabled/due/run shape:
    `run()` is blocking and never raises — the lifespan loop calls it via
    asyncio.to_thread."""

    def __init__(self, interval_s: int, fetch=None):
        self.enabled = interval_s > 0
        self.interval = max(interval_s, _MIN_INTERVAL_S) if self.enabled else 0
        self._fetch = fetch or _fetch_sso_status
        self._next = 0.0                    # first due() fires immediately

    def due(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) >= self._next

    def run(self) -> None:   # blocking: call via asyncio.to_thread
        t0 = time.monotonic()
        self._next = t0 + self.interval
        try:
            status = self._fetch()
            log("sso-probe", status=status, ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:  # noqa: BLE001 - a diagnostic must never take the loop down
            log("sso-probe", status=None, error=type(e).__name__,
                ms=int((time.monotonic() - t0) * 1000))


def _fetch_sso_status() -> int:
    # curl_cffi ships with garminconnect (already a gateway dependency). The
    # probe must present the same browser fingerprint the real sign-in does —
    # a plain client would measure generic bot blocking instead of ours.
    from curl_cffi import requests as cr
    r = cr.get(_SSO_PAGE, impersonate="chrome", timeout=_FETCH_TIMEOUT_S)
    return r.status_code
