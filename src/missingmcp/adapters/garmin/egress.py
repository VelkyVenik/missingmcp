from __future__ import annotations
import collections
import contextlib
import hashlib
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from garminconnect import client as _gc_client

# Reliability ticket 12: Cloudflare in front of Garmin's SSO rate-limits the
# gateway's shared Railway egress IP, while the same request from a clean IP
# gets through. GARMIN_SSO_PROXY lists proxies on dedicated IPs; each account
# signs in through one of them (sticky by account hash), and each egress —
# every proxy plus the direct path, kept as the last resort — has its own
# circuit breaker, so one blocked IP sheds its accounts onto the others.
# Direct stays last on purpose: it is only reached when every proxy is
# cooling down (0 sign-ins took it in the first 16 h), and without it two
# tripped proxies would mean no sign-ins at all for the 30 min cooldown.
#
# garminconnect takes no proxy option and builds its HTTP sessions internally,
# so the two HTTP modules garminconnect.client uses are swapped for thin
# wrappers that inject the proxy chosen for the *current thread* (`via()`)
# into every Session/post they create. Sign-in runs on asyncio.to_thread
# workers, one call per thread, so concurrent sign-ins on different egresses
# never see each other's proxy. In the gateway process garminconnect serves
# sign-in only — workers are separate processes (unaffected), and
# WHOOP/PostHog/S3 use other clients — so nothing else rides the proxies.


# When Garmin's Cloudflare rate-limits an egress IP, every sign-in attempt
# through it burns ~30s cycling login strategies before failing "blocked" —
# and those very attempts keep the rate limiter hot. Once enough distinct
# accounts report blocked on one egress, that egress is skipped for a
# cooldown: its accounts move to the next proxy, and our footprint on the
# blocked IP drops to zero so the block can decay. 30 min (not 5): a burned
# proxy that reopens after 5 min just re-attracts its sticky accounts and
# fails them again (observed 2026-10-05 on one of the two Hetzner ports).
_SSO_BREAKER_COOLDOWN_S = 1800.0

# Garmin also rate-limits repeat sign-ins of the *same account*, regardless of
# IP: right after a success, a second sign-in of that account a minute later
# comes back "blocked" while other accounts sail through the same egress
# (observed 2026-10-04 on both dedicated IPs). A lone blocked account must not
# take a whole egress down for everyone, so "blocked" first cools down only
# the account; the egress breaker trips once several distinct accounts are
# blocked on it within a window — the signature of an IP-level limit.
# 5 min (was 60 s): users retried about once a minute, each retry another
# full round of SSO requests from our IP — the repeat-heavy egress was the one
# Cloudflare burned on 2026-10-05; the form copy already says "a couple of
# minutes".
ACCOUNT_COOLDOWN_S = 300.0
_EGRESS_BLOCK_WINDOW_S = 600.0
_EGRESS_BLOCK_ACCOUNTS = 2

# failing-logins map, "Choose the throttle policy" (2026-10-08):
# - post-connect hold: a just-connected login email can't start another
#   sign-in for 2 min (double submits / a second device sent Garmin a repeat
#   sign-in; 12 % of blocks hit an email that had just got through);
# - an egress also trips on >= 10 blocked in the last hour, staying under
#   Cloudflare's stock login rule (~20 failures/h per IP -> 1-day block);
# - egress rest escalates 30 min -> 1 h -> 2 h while an egress keeps getting
#   tripped, back to the first step once a sign-in gets through it (burned
#   egresses recovered in ~1-2 h; a 30 min rest re-tripped them).
POST_CONNECT_HOLD_S = 120.0
_EGRESS_HOURLY_BLOCKS = 10
_EGRESS_REST_STEPS_S = (1800.0, 3600.0, 7200.0)


class SsoBreaker:
    """Process-local circuit breaker for Garmin SSO sign-in through one egress.

    Time-based and deliberately tiny: `trip()` opens it for `cooldown` seconds,
    `remaining()` says how long it stays open; expiry alone closes it (the next
    attempt after the cooldown is the half-open probe). Thread-safe because
    `start_login` runs on `asyncio.to_thread` workers — including *abandoned*
    ones whose authorize POST already timed out: when such a thread finally
    fails "blocked", its trip() is fresh evidence the egress is still blocked,
    arriving exactly when the next request needs it. Process-local state is
    fine here for the same reason it is for `RateLimiter` (single-node)."""

    def __init__(self, cooldown: float = _SSO_BREAKER_COOLDOWN_S, clock=time.monotonic):
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._open_until = 0.0

    def trip(self, seconds: float | None = None) -> None:
        with self._lock:
            self._open_until = self._clock() + (self.cooldown if seconds is None else seconds)

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - self._clock())


def describe(url: str | None) -> str:
    """Log-safe label of an egress: host:port of a proxy URL (the URL usually
    carries proxy credentials, which must never reach the logs), or "direct"."""
    if not url:
        return "direct"
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


@dataclass
class Route:
    label: str             # log-safe: "direct" or host:port
    proxy: str | None      # None = the host's own egress
    breaker: SsoBreaker
    rest_s: float = 0.0    # length of the latest egress rest (for logs)
    # The address Garmin actually sees, learned by the probe (cdn-cgi/trace).
    # The label shows the *proxy* host — every port on the box shares it,
    # while each port leaves through its own IP (ticket 12: that mix-up made
    # a burned floating IP look like the primary one).
    egress_ip: str | None = None


class EgressPool:
    """The sign-in egresses: each GARMIN_SSO_PROXY entry, then direct."""

    def __init__(self, spec: str = "", breaker=SsoBreaker, clock=time.monotonic):
        proxies = [p.strip() for p in spec.split(",") if p.strip()]
        self.proxied = proxies
        self.routes = [Route(describe(p), p, breaker()) for p in proxies]
        self.routes.append(Route("direct", None, breaker()))
        self._clock = clock
        self._lock = threading.Lock()
        self._account_until: dict[str, float] = {}
        # per egress label: account -> when it was last blocked there
        self._blocked: dict[str, dict[str, float]] = {r.label: {} for r in self.routes}
        self._held_until: dict[str, float] = {}       # post-connect hold
        self._in_flight: set[str] = set()              # one sign-in per email
        self._hourly: dict[str, collections.deque] = {r.label: collections.deque()
                                                      for r in self.routes}
        self._rest_level: dict[str, int] = {r.label: 0 for r in self.routes}
        # per egress label: sign-in outcomes since the last take_stats()
        self._stats: dict[str, collections.Counter] = {
            r.label: collections.Counter() for r in self.routes}

    def order(self, account_key: str) -> list[Route]:
        """Every route in this account's preference order: its sticky proxy
        first (stable hash, so one account keeps one IP and accounts spread
        evenly), the other proxies after it, direct last."""
        n = len(self.proxied)
        if not n:
            return list(self.routes)
        start = int(hashlib.sha256(account_key.encode()).hexdigest(), 16) % n
        proxied = self.routes[:n]
        return proxied[start:] + proxied[:start] + self.routes[n:]

    def pick(self, account_key: str) -> Route | None:
        """The first route whose breaker is closed, or None if all are open."""
        return next((r for r in self.order(account_key) if r.breaker.remaining() <= 0), None)

    def min_remaining(self) -> float:
        return min(r.breaker.remaining() for r in self.routes)

    def account_remaining(self, account_key: str) -> float:
        with self._lock:
            return max(0.0, self._account_until.get(account_key, 0.0) - self._clock())

    def begin(self, account_key: str) -> bool:
        """Claim the in-flight slot for this login email; False if a sign-in
        for it is already running at Garmin (even one whose form timed out)."""
        with self._lock:
            if account_key in self._in_flight:
                return False
            self._in_flight.add(account_key)
            return True

    def end(self, account_key: str) -> None:
        with self._lock:
            self._in_flight.discard(account_key)

    def mark_connected(self, account_key: str) -> None:
        """Start the post-connect hold — only on a real connection (signed in
        or MFA finished), never on reaching MFA: a wrong password lands there
        and must be able to go back and retry at once."""
        now = self._clock()
        with self._lock:
            for acct, until in list(self._held_until.items()):
                if until <= now:
                    del self._held_until[acct]
            self._held_until[account_key] = now + POST_CONNECT_HOLD_S

    def hold_remaining(self, account_key: str) -> float:
        with self._lock:
            return max(0.0, self._held_until.get(account_key, 0.0) - self._clock())

    def note(self, route: Route, outcome: str) -> None:
        """Count one sign-in outcome on this egress (for egress-health)."""
        with self._lock:
            self._stats[route.label][outcome] += 1

    def take_stats(self) -> dict[str, collections.Counter]:
        """Outcome counts per egress since the previous call, then reset."""
        with self._lock:
            out = self._stats
            self._stats = {r.label: collections.Counter() for r in self.routes}
        return out

    def record_ok(self, route: Route) -> None:
        """A sign-in got through this egress: blocks counted so far were
        account-level (Garmin let someone else in), so forget them. Without
        this, routine per-account blocks on a healthy IP would add up to an
        egress trip and send everyone to direct for the whole cooldown."""
        with self._lock:
            self._blocked[route.label].clear()
            self._rest_level[route.label] = 0

    def record_blocked(self, route: Route, account_key: str) -> str:
        """Note a "blocked" sign-in; return its scope. Always cools the account
        down; trips the route's breaker ("egress") once enough distinct
        accounts were blocked on it within the window with no sign-in getting
        through in between (see record_ok), or once it collected
        _EGRESS_HOURLY_BLOCKS blocks in the last hour; else "account". A trip
        rests the egress for the next step of _EGRESS_REST_STEPS_S."""
        now = self._clock()
        with self._lock:
            for acct, until in list(self._account_until.items()):
                if until <= now:
                    del self._account_until[acct]
            self._account_until[account_key] = now + ACCOUNT_COOLDOWN_S
            recent = self._blocked[route.label]
            for acct, at in list(recent.items()):
                if now - at > _EGRESS_BLOCK_WINDOW_S:
                    del recent[acct]
            recent[account_key] = now
            hourly = self._hourly[route.label]
            hourly.append(now)
            while hourly and now - hourly[0] > 3600:
                hourly.popleft()
            tripped = (len(recent) >= _EGRESS_BLOCK_ACCOUNTS
                       or len(hourly) >= _EGRESS_HOURLY_BLOCKS)
            if tripped:
                recent.clear()
                hourly.clear()
                level = self._rest_level[route.label]
                rest = _EGRESS_REST_STEPS_S[min(level, len(_EGRESS_REST_STEPS_S) - 1)]
                self._rest_level[route.label] = level + 1
        if tripped:
            route.rest_s = rest
            route.breaker.trip(rest)
            return "egress"
        return "account"


_local = threading.local()


@contextlib.contextmanager
def via(proxy: str | None):
    """Route garminconnect traffic created on this thread through `proxy`."""
    prev = getattr(_local, "proxy", None)
    _local.proxy = proxy
    try:
        yield
    finally:
        _local.proxy = prev


def current() -> str | None:
    return getattr(_local, "proxy", None)


class _ProxiedRequests:
    """`requests` stand-in: sessions and module-level posts get the proxy."""

    def __init__(self, mod):
        self._mod = mod

    def __getattr__(self, name):
        return getattr(self._mod, name)

    def Session(self, *args, **kwargs):  # noqa: N802 - mirrors requests.Session
        s = self._mod.Session(*args, **kwargs)
        proxy = current()
        if proxy:
            s.proxies.update({"http": proxy, "https": proxy})
            # requests merges env proxies (HTTPS_PROXY…) *over* Session.proxies;
            # the chosen egress must win, or a stray env var would carry the
            # sign-in elsewhere while logs and breakers blame this route.
            s.trust_env = False
        return s

    def post(self, url, **kwargs):
        proxy = current()
        if proxy:
            kwargs.setdefault("proxies", {"http": proxy, "https": proxy})
        return self._mod.post(url, **kwargs)


class _ProxiedCffi:
    """`curl_cffi.requests` stand-in, same contract via curl_cffi's `proxy=`."""

    def __init__(self, mod):
        self._mod = mod

    def __getattr__(self, name):
        return getattr(self._mod, name)

    def Session(self, *args, **kwargs):  # noqa: N802 - mirrors curl_cffi Session
        proxy = current()
        if proxy:
            kwargs.setdefault("proxy", proxy)
        return self._mod.Session(*args, **kwargs)

    def post(self, url, **kwargs):
        proxy = current()
        if proxy:
            kwargs.setdefault("proxy", proxy)
        return self._mod.post(url, **kwargs)


def _install() -> None:
    if not isinstance(_gc_client.requests, _ProxiedRequests):
        _gc_client.requests = _ProxiedRequests(_gc_client.requests)
    cffi = getattr(_gc_client, "cffi_requests", None)
    if cffi is not None and not isinstance(cffi, _ProxiedCffi):
        _gc_client.cffi_requests = _ProxiedCffi(cffi)


_install()
