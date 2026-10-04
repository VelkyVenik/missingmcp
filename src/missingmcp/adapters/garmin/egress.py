from __future__ import annotations
from urllib.parse import urlsplit

from garminconnect import client as _gc_client

# Reliability ticket 12: Cloudflare in front of Garmin's SSO rate-limits the
# gateway's shared Railway egress IP, while the same request from a clean IP
# gets through. GARMIN_SSO_PROXY sends the gateway's own garminconnect traffic
# (sign-in, MFA resume, token verify) out through a dedicated IP instead.
#
# garminconnect takes no proxy option and builds its HTTP sessions internally,
# so the two HTTP modules garminconnect.client uses are swapped for thin
# wrappers that inject the proxy into every Session/post they create. In the
# gateway process garminconnect serves sign-in only — workers are separate
# processes (unaffected), and WHOOP/PostHog/S3 use other clients — so nothing
# else rides the proxy. The proxy is read per call: None means pass-through,
# i.e. exactly the library's own behaviour.

_proxy: str | None = None


def configure(url: str | None) -> None:
    global _proxy
    _proxy = url or None


def current() -> str | None:
    return _proxy


def describe(url: str | None) -> str | None:
    """host:port of a proxy URL — the only part of it fit for logs (the URL
    usually carries proxy credentials)."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


class _ProxiedRequests:
    """`requests` stand-in: sessions and module-level posts get the proxy."""

    def __init__(self, mod):
        self._mod = mod

    def __getattr__(self, name):
        return getattr(self._mod, name)

    def Session(self, *args, **kwargs):  # noqa: N802 - mirrors requests.Session
        s = self._mod.Session(*args, **kwargs)
        if _proxy:
            s.proxies.update({"http": _proxy, "https": _proxy})
        return s

    def post(self, url, **kwargs):
        if _proxy:
            kwargs.setdefault("proxies", {"http": _proxy, "https": _proxy})
        return self._mod.post(url, **kwargs)


class _ProxiedCffi:
    """`curl_cffi.requests` stand-in, same contract via curl_cffi's `proxy=`."""

    def __init__(self, mod):
        self._mod = mod

    def __getattr__(self, name):
        return getattr(self._mod, name)

    def Session(self, *args, **kwargs):  # noqa: N802 - mirrors curl_cffi Session
        if _proxy:
            kwargs.setdefault("proxy", _proxy)
        return self._mod.Session(*args, **kwargs)

    def post(self, url, **kwargs):
        if _proxy:
            kwargs.setdefault("proxy", _proxy)
        return self._mod.post(url, **kwargs)


def _install() -> None:
    if not isinstance(_gc_client.requests, _ProxiedRequests):
        _gc_client.requests = _ProxiedRequests(_gc_client.requests)
    cffi = getattr(_gc_client, "cffi_requests", None)
    if cffi is not None and not isinstance(cffi, _ProxiedCffi):
        _gc_client.cffi_requests = _ProxiedCffi(cffi)


_install()
