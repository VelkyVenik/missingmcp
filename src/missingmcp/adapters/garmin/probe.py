from __future__ import annotations
import time
from ...log import log

# The SSO embed widget — the page of the sign-in strategy that actually gets
# through from a clean egress (widget+cffi). Its status per egress is the
# signal being graphed (reliability ticket 12). Not the portal sign-in page:
# Cloudflare answers that one 403 even from IPs where sign-in succeeds, so it
# false-alarmed on every proxy (2026-10-04; the probe used it until then).
_SSO_PAGE = "https://sso.garmin.com/sso/embed"
# Cloudflare's trace endpoint on the same host echoes the caller's address
# (`ip=…`): how the probe learns which IP each egress really leaves from.
_TRACE_PAGE = "https://sso.garmin.com/cdn-cgi/trace"
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
    the request itself failed) and the egress's real outgoing IP.

    The embed GET can't see a block on the credential POST (2026-10-05: 200
    on an egress where 55 of 56 sign-ins were blocked), and an active
    credential probe would mean fake failed sign-ins from our IPs — the very
    signature that burns them. So with a pool, each run also emits
    `egress-health` per egress: the *real* sign-in outcomes since the last
    run (passive, no extra Garmin traffic). Follows backup.Backup's
    enabled/due/run shape:
    `run()` is blocking and never raises — the lifespan loop calls it via
    asyncio.to_thread."""

    def __init__(self, interval_s: int, fetch=None, routes=None, pool=None, trace=None):
        self.enabled = interval_s > 0
        # One probe per sign-in egress (egress.EgressPool.routes), so a
        # blocked proxy IP is visible before users hit it; `via` labels each.
        self._pool = pool
        self._routes = list(pool.routes if pool else routes or [])
        self._targets = ([(r.label, r.proxy) for r in self._routes]
                         if self._routes else [("direct", None)])
        self._trace = trace or _fetch_egress_ip
        self.interval = max(interval_s, _MIN_INTERVAL_S) if self.enabled else 0
        self._fetch = fetch or _fetch_sso_status
        self._next = 0.0                    # first due() fires immediately
        self._last_run = 0.0

    def due(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) >= self._next

    def run(self) -> None:   # blocking: call via asyncio.to_thread
        t0 = time.monotonic()
        window = int(t0 - self._last_run) if self._last_run else None
        self._last_run = t0
        self._next = t0 + self.interval
        by_label = {r.label: r for r in self._routes}
        for label, proxy in self._targets:
            route = by_label.get(label)
            ip = self._learn_ip(route, proxy)
            t1 = time.monotonic()
            try:
                status = self._fetch(proxy)
                log("sso-probe", status=status, ms=int((time.monotonic() - t1) * 1000),
                    via=label, egress_ip=ip, message=f"sso-probe via {label}: {status}")
            except Exception as e:  # noqa: BLE001 - a diagnostic must never take the loop down
                log("sso-probe", status=None, error=type(e).__name__,
                    ms=int((time.monotonic() - t1) * 1000), via=label, egress_ip=ip,
                    message=f"sso-probe via {label}: {type(e).__name__}")
        if self._pool is not None:
            stats = self._pool.take_stats()
            for r in self._routes:
                c = stats.get(r.label) or {}
                good = c.get("ok", 0) + c.get("needs_mfa", 0)
                blocked = c.get("blocked", 0)
                attempts = sum(c.values())
                log("egress-health", via=r.label, egress_ip=r.egress_ip,
                    window_s=window, attempts=attempts, ok=c.get("ok", 0),
                    needs_mfa=c.get("needs_mfa", 0), blocked=blocked,
                    other=attempts - good - blocked,
                    message=(f"egress-health {r.label} ({r.egress_ip or '?'}): "
                             f"{good} through / {blocked} blocked of {attempts}"))

    def _learn_ip(self, route, proxy) -> str | None:
        """The egress's real outgoing IP; keeps the last known one on failure."""
        try:
            ip = self._trace(proxy)
        except Exception:  # noqa: BLE001 - diagnostic only
            ip = None
        if route is None:
            return ip
        if ip and ip != route.egress_ip:
            if route.egress_ip:
                log("egress-ip-changed", via=route.label, egress_ip=ip,
                    previous=route.egress_ip,
                    message=f"egress {route.label} now leaves from {ip} (was {route.egress_ip})")
            route.egress_ip = ip
        return route.egress_ip


def _fetch_sso_status(proxy: str | None = None) -> int:
    # curl_cffi ships with garminconnect (already a gateway dependency). The
    # probe must present the same browser fingerprint the real sign-in does —
    # a plain client would measure generic bot blocking instead of ours.
    from curl_cffi import requests as cr
    kwargs = {"proxy": proxy} if proxy else {}
    r = cr.get(_SSO_PAGE, impersonate="chrome", timeout=_FETCH_TIMEOUT_S, **kwargs)
    return r.status_code


def _fetch_egress_ip(proxy: str | None = None) -> str | None:
    from curl_cffi import requests as cr
    kwargs = {"proxy": proxy} if proxy else {}
    r = cr.get(_TRACE_PAGE, impersonate="chrome", timeout=_FETCH_TIMEOUT_S, **kwargs)
    for line in r.text.splitlines():
        if line.startswith("ip="):
            return line[3:].strip() or None
    return None
