"""PostHog telemetry: product/campaign events via the official `posthog` SDK
plus an OTLP tee of the structured log stream (design:
docs/superpowers/specs/2026-07-20-posthog-telemetry-design.md).

Everything here is env-gated and fire-and-forget: without POSTHOG_API_KEY every
function is a cheap no-op, and a PostHog outage must never block a request,
fail a login, or crash the process — the SDK queues in a daemon thread and
drops on overflow, the OTLP exporter batches and drops the same way, and every
call site is wrapped so an SDK bug can't propagate.

Egress rule (spec, ticket 03): identity and metadata yes, content never.
Callers pass explicit property dicts — never request bodies, credentials, or
form contents. The account email travels only as distinct_id.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Mapping

from . import log as _log

_client = None            # posthog.Posthog | None — None means disabled
_api_key = ""
_logger_provider = None   # OTel LoggerProvider, kept for shutdown flush

# Loggers whose bridged records must NOT re-enter the tee: the OTLP exporter
# logs its own failures (opentelemetry.*) over an HTTP stack that logs too
# (urllib3/requests, shared with the posthog SDK) — teeing those would turn an
# export failure into a self-amplifying loop. They still reach stdout/Railway.
_TEE_SKIP_PREFIXES = ("opentelemetry", "urllib3", "requests", "posthog")

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING,
           "error": logging.ERROR, "critical": logging.CRITICAL}


def enabled() -> bool:
    return _client is not None


def init(config) -> None:
    """Create the SDK client and attach the OTLP log tee. No-op without a key.
    Imports are deferred so the (heavy) SDK/OTel stacks load only when used."""
    global _client, _api_key
    if not config.posthog_api_key or _client is not None:
        return
    from posthog import Posthog
    _api_key = config.posthog_api_key
    _client = Posthog(config.posthog_api_key, host=config.posthog_host)
    _attach_log_tee(config)
    _log.log("telemetry-enabled", host=config.posthog_host)


def shutdown() -> None:
    """Best-effort flush of both queues. Blocking — call via asyncio.to_thread."""
    global _client, _logger_provider, _api_key
    _log.set_sink(None)
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass
        _client = None
    if _logger_provider is not None:
        try:
            _logger_provider.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _logger_provider = None
    _api_key = ""


def capture(event: str, *, distinct_id: str | None = None,
            properties: dict[str, Any] | None = None,
            anonymous: bool = False) -> None:
    """Non-blocking enqueue of one event. distinct_id=None → personless event
    (SDK generates a UUID); anonymous=True keeps the event out of person
    profiles (and on anonymous-event pricing)."""
    if _client is None:
        return
    props = dict(properties or {})
    if anonymous:
        props["$process_person_profile"] = False
    try:
        _client.capture(event, distinct_id=distinct_id, properties=props)
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        pass


def identify(email: str, anon_distinct_id: str) -> None:
    """Stitch the anonymous web visitor (posthog-js cookie) to the account —
    the server-side merge decided in ticket 03."""
    capture("$identify", distinct_id=email,
            properties={"$anon_distinct_id": anon_distinct_id})


def anon_id_from_cookie(cookies: Mapping[str, str]) -> str | None:
    """The posthog-js anonymous distinct_id, from the `ph_<key>_posthog`
    cookie the browser sends with OAuth form POSTs (same domain as the
    landing pages). None when absent/unparseable — stitching is best-effort."""
    if not _api_key:
        return None
    raw = cookies.get(f"ph_{_api_key}_posthog")
    if not raw:
        return None
    try:
        data = json.loads(urllib.parse.unquote(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    did = data.get("distinct_id")
    return did if isinstance(did, str) and did else None


# --- web analytics (posthog-js) ---------------------------------------------

def asset_host(host: str) -> str:
    """posthog-js is served from the region's assets CDN; a managed reverse
    proxy (e.g. j.missingmcp.com) serves it itself, so the host passes through
    unchanged — the same rewrite rule the official snippet applies."""
    return (host.replace("eu.i.posthog.com", "eu-assets.i.posthog.com")
                .replace("us.i.posthog.com", "us-assets.i.posthog.com"))


def web_head(config) -> str:
    """<head> tags for pages: load posthog-js (via the web host — the managed
    reverse proxy in production), then our same-origin bootstrap (CSP forbids
    inline scripts; `defer` preserves order). Empty when telemetry is off."""
    if _client is None:
        return ""
    return (f'<script defer src="{asset_host(config.posthog_web_host)}/static/array.js"></script>\n'
            '  <script defer src="/static/ph.js"></script>')


def web_bootstrap_js(config) -> str:
    """The same-origin posthog-js init (served at /static/ph.js). OAuth/sign-in
    pages carry credential forms, so they get explicit pageviews only —
    autocapture stays on the marketing pages (spec, ticket 03). ui_host points
    generated links back at the PostHog app when api_host is the proxy."""
    opts = {
        "api_host": config.posthog_web_host,
        "defaults": "2026-05-30",
        "person_profiles": "identified_only",
        "cookieless_mode": "on_reject",
    }
    if ".i.posthog.com" not in config.posthog_web_host:  # behind a reverse proxy
        opts["ui_host"] = config.posthog_ui_host
    return f"""(function () {{
  if (!window.posthog || !window.posthog.init) return;
  var oauthPage = location.pathname.indexOf('/oauth/') !== -1;
  var opts = {json.dumps(opts, indent=2)};
  opts.autocapture = !oauthPage;
  opts.capture_pageleave = !oauthPage;
  window.posthog.init({json.dumps(config.posthog_api_key)}, opts);
}})();
"""


# --- OTLP log tee ------------------------------------------------------------

def _tee_wanted(record: dict) -> bool:
    """Skip records bridged from the export path's own loggers (loop guard) —
    everything else that hits stdout ships to PostHog Logs verbatim."""
    return not str(record.get("logger", "")).startswith(_TEE_SKIP_PREFIXES)


def _attach_log_tee(config) -> None:
    """Tee the structured stdout stream to PostHog Logs over OTLP. The tee
    logger never propagates to the root logger (whose _StructuredHandler
    would loop it straight back into the sink)."""
    global _logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    provider = LoggerProvider(resource=Resource.create({"service.name": "missingmcp"}))
    exporter = OTLPLogExporter(
        endpoint=f"{config.posthog_host}/i/v1/logs",
        headers={"Authorization": f"Bearer {config.posthog_api_key}"},
    )
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    tee = logging.getLogger("missingmcp.telemetry.tee")
    tee.propagate = False
    tee.setLevel(logging.DEBUG)
    tee.handlers = [LoggingHandler(level=logging.DEBUG, logger_provider=provider)]
    _logger_provider = provider

    def sink(record: dict) -> None:
        if _tee_wanted(record):
            tee.log(_LEVELS.get(str(record.get("level")), logging.INFO),
                    json.dumps(record))

    _log.set_sink(sink)
