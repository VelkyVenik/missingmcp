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
    """<head> tag for pages: the same-origin bootstrap only (CSP forbids inline
    scripts). The bootstrap embeds PostHog's official loader, which injects
    array.js itself — array.js loaded standalone does NOT create
    `window.posthog` (verified 2026-07-21), so the loader is mandatory.
    Empty when telemetry is off."""
    if _client is None:
        return ""
    return '<script defer src="/static/ph.js"></script>'


# PostHog's official loader stub, verbatim from the project's install page: it
# creates the queueing `window.posthog` stub and injects array.js from
# api_host (rewriting *.i.posthog.com to the assets CDN; a reverse-proxy host
# passes through unchanged, so CSP script-src needs only the web host).
_PH_LOADER = r"""!function(t,e){var o,n,p,r;e.__SV||(window.posthog && window.posthog.__loaded)||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="Ji Yi init fn mn Ur pn bn cn capture calculateEventProperties Sn register register_once register_for_session unregister unregister_for_session Tn getFeatureFlag getFeatureFlagPayload getFeatureFlagResult getAllFeatureFlags isFeatureEnabled reloadFeatureFlags updateFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSurveysLoaded onSessionId getSurveys getActiveMatchingSurveys renderSurvey displaySurvey cancelPendingSurvey canRenderSurvey canRenderSurveyAsync Mn identify setPersonProperties unsetPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset shutdown setIdentity clearIdentity get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException addExceptionStep captureLog startExceptionAutocapture stopExceptionAutocapture loadToolbar get_property getSessionProperty Cn xn createPersonProfile setInternalOrTestUser In hn Pn opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing get_explicit_consent_status is_capturing clear_opt_in_out_capturing debug Vr Rt getPageViewId captureTraceFeedback captureTraceMetric an".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);"""


def web_bootstrap_js(config) -> str:
    """The same-origin posthog-js bootstrap (served at /static/ph.js): the
    official loader followed by init. No cookieless/consent mode in v1 —
    `cookieless_mode: 'on_reject'` captures NOTHING until an explicit
    opt-in/out call, so it must ship together with a consent banner (spec
    follow-up; smoke-tested 2026-07-21). OAuth/sign-in pages carry credential
    forms, so they get explicit pageviews only — autocapture stays on the
    marketing pages (spec, ticket 03). ui_host points generated links back at
    the PostHog app when api_host is the proxy."""
    opts = {
        "api_host": config.posthog_web_host,
        "defaults": "2026-05-30",
        "person_profiles": "identified_only",
    }
    if ".i.posthog.com" not in config.posthog_web_host:  # behind a reverse proxy
        opts["ui_host"] = config.posthog_ui_host
    init = f"""
(function () {{
  var oauthPage = location.pathname.indexOf('/oauth/') !== -1;
  var opts = {json.dumps(opts, indent=2)};
  opts.autocapture = !oauthPage;
  opts.capture_pageleave = !oauthPage;
  window.posthog.init({json.dumps(config.posthog_api_key)}, opts);
}})();
"""
    return _PH_LOADER + init


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
