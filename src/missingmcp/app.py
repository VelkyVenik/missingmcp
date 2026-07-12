from __future__ import annotations
import asyncio
import contextlib
import json
import os
import urllib.parse
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware
from . import backup, store, oauth, pages, proxy, security
from .config import load_config, Config
from .workers import WorkerManager
from .adapters import build_adapters, RETIRED_ADAPTERS
from .adapters.base import is_remote, is_local, is_upstream_oauth
from .log import log

_TPL = Path(__file__).parent / "templates"
_STATIC = Path(__file__).parent / "static"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        for k, v in security.security_headers().items():
            resp.headers.setdefault(k, v)
        return resp


def _run_data_cleanup(conn, orphan_ttl: int, retired_adapters) -> None:
    """One pass of data-hygiene cleanup, driven from the lifespan loop: fully
    purge any retired adapter's rows, then sweep abandoned OAuth clients (0 tokens,
    older than orphan_ttl). Purge runs first so a retired adapter's clients are
    reported under the purge, not double-counted as orphans. Logs only when it
    actually deleted something — the loop runs every 60s and is usually a no-op."""
    for name in retired_adapters:
        counts = store.purge_adapter(conn, name)
        if any(counts.values()):
            log("cleanup-dead-adapter", adapter=name, **counts)
    n = store.cleanup_orphan_clients(conn, orphan_ttl)
    if n:
        log("cleanup-orphan-clients", count=n)


def build_app(config: Config) -> Starlette:
    conn = store.init_db(config.db_path)
    adapters = build_adapters(config)
    # One WorkerManager per worker-based adapter; remote and local adapters need none.
    # NOTE: managers share one port range, one DATA_DIR/users/<key> namespace and
    # one workers.json snapshot — safe while garmin is the only worker-based
    # adapter; scope those per adapter before ever adding a second one.
    managers = {a.name: WorkerManager(config, a.forward)
                for a in adapters.values()
                if not is_remote(a.forward) and not is_local(a.forward)}
    auth_state = oauth.AuthState(security.CsrfStore())
    rate = security.RateLimiter()
    bk = backup.Backup(config)

    def _render(name: str, title: str, desc: str | None = None,
                path: str = "/", extra_head: str = "") -> str:
        return pages.render_page(
            name, title, desc, public_url=config.public_url, path=path,
            extra_head=extra_head,
        ).replace(
            "{PUBLIC_URL}", config.public_url
        ).replace("{OPERATOR}", pages.operator_html(config)).replace(
            "{OPERATOR_EMAIL}", f" ({config.operator_email})" if config.operator_email else ""
        )

    def _json_ld(data: dict) -> str:
        # ld+json is a data block, never executed — CSP script-src doesn't apply
        return ('<script type="application/ld+json">'
                + json.dumps({"@context": "https://schema.org", **data})
                + "</script>")

    home_page = _render(
        "home.html", "MissingMCP — Your data, in Claude · Garmin MCP Server",
        "Give Claude your Garmin and health data, then just ask — did I eat "
        "enough for today's ride, how did I sleep this week? A hosted Garmin "
        "MCP server: connect in two minutes. Free and open source.",
        extra_head=_json_ld({"@type": "WebSite", "name": "MissingMCP",
                             "url": config.public_url}))

    async def home(request):
        return HTMLResponse(home_page)

    privacy_page = _render(
        "privacy.html", "Privacy — MissingMCP",
        "What MissingMCP stores, what it never stores, and how to revoke "
        "access or delete your data.")

    async def privacy(request):
        return HTMLResponse(privacy_page)

    async def subscribe(request):
        ip = request.client.host if request.client else "unknown"
        if not rate.check(f"subscribe:{ip}", 5, 60):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await security.read_body_limited(request, max_bytes=64_000)
        if raw is None:
            return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        if (form.get("website", [""])[0] or "").strip():   # honeypot: bots fill it
            return JSONResponse({"ok": True})               # look successful, store nothing
        email = (form.get("email", [""])[0] or "").strip().lower()
        if not security.valid_email(email):
            return JSONResponse({"ok": False, "error": "invalid_email"}, status_code=400)
        store.add_subscriber(conn, email)
        log("subscribe")                                # never log the address itself
        return JSONResponse({"ok": True})

    async def suggest(request):
        ip = request.client.host if request.client else "unknown"
        if not rate.check(f"suggest:{ip}", 5, 60):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await security.read_body_limited(request, max_bytes=64_000)
        if raw is None:
            return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        if (form.get("website", [""])[0] or "").strip():
            return JSONResponse({"ok": True})
        email = (form.get("email", [""])[0] or "").strip().lower()
        if not security.valid_email(email):
            return JSONResponse({"ok": False, "error": "invalid_email"}, status_code=400)
        description = (form.get("description", [""])[0] or "").strip()[:2000]
        wants = (form.get("wants_updates", [""])[0] or "").lower() in ("1", "true", "on", "yes")
        store.add_suggestion(conn, email, description, wants)
        log("suggest", wants_updates=wants)
        return JSONResponse({"ok": True})

    async def notfound(request):
        # Catch-all for unknown GET paths: humans get the MissingMCP home
        # (with links to every connector) but with a 404 status so
        # API/discovery clients still read it as "not here".
        return HTMLResponse(home_page, status_code=404)

    favicon_svg = (_TPL / "favicon.svg").read_text()
    # Brand assets (the MissingMCP logo mark). Read once at startup and served
    # from memory, same as the favicon — same-origin, so CSP `default-src 'self'`
    # covers them without an img-src rule.
    _assets = {n: (_STATIC / n).read_bytes()
               for n in ("icon.png", "favicon-32.png", "apple-touch-icon.png")}
    site_js = (_STATIC / "site.js").read_text()

    async def healthz(request):
        return PlainTextResponse("ok")

    async def favicon(request):
        return Response(favicon_svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    # --- crawler & AI-assistant surface, generated from the adapter registry
    base = config.public_url
    robots_txt = ("User-agent: *\nAllow: /\n"
                  + "".join(f"Disallow: /{a.name}/oauth/\n" for a in adapters.values())
                  + f"Sitemap: {base}/sitemap.xml\n")
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{base}{path}</loc></url>\n"
                  for path in ["/"] + [f"/{a.name}" for a in adapters.values()])
        + "</urlset>\n")
    llms_txt = (
        "# MissingMCP\n\n"
        "> Hosted MCP connectors for Claude and other MCP clients — currently a "
        "Garmin MCP server, with more services on the way. Users sign in once with "
        "their own account; the gateway handles OAuth 2.1 (PKCE, dynamic client "
        "registration) and forwards MCP traffic to a per-user upstream.\n\n"
        "## Connectors\n"
        + "".join(
            f"- [{a.display_name}]({base}/{a.name}): hosted {a.display_name} MCP "
            f"server. MCP endpoint: {base}/{a.name}/mcp (Streamable HTTP, OAuth 2.1 "
            f"— sign in with your own {a.display_name} account).\n"
            for a in adapters.values())
        + "\n## How to connect\n"
        "- In Claude: Settings → Connectors → Add custom connector → paste the "
        "MCP endpoint URL above.\n\n"
        "## Source\n"
        "- Gateway: https://github.com/VelkyVenik/missingmcp\n"
        "- Garmin worker: https://github.com/Taxuspt/garmin_mcp\n")
    # Glama connector-directory ownership proof (glama.ai/mcp/connectors):
    # the email must match the operator's Glama account email.
    glama_json = ('{"$schema": "https://glama.ai/mcp/schemas/server.json",\n'
                  ' "maintainers": ["vaclav@slajs.eu"]}\n')

    def _text(body: str, media_type: str):
        async def handler(request):
            return Response(body, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=3600"})
        return handler

    def static_png(name):
        body = _assets[name]

        async def handler(request):
            return Response(body, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})
        return handler

    def adapter_routes(adapter):
        p = adapter.name
        manager = managers.get(p)          # None for remote-forward adapters
        # SEO pattern scales per adapter: "<Name> MCP Server" is the query
        # people actually search for ("garmin mcp", "whoop mcp", ...).
        landing_desc = (f"Hosted {adapter.display_name} MCP server: connect your "
                        f"{adapter.display_name} account to Claude in two minutes. "
                        "Sign in once, add a URL, start asking. Free and open source.")
        landing_page = _render(
            adapter.landing_template,
            f"{adapter.display_name} MCP Server — Connect {adapter.display_name} "
            "to Claude | MissingMCP",
            landing_desc, path=f"/{p}",
            extra_head=_json_ld({
                "@type": "SoftwareApplication",
                "name": f"{adapter.display_name} MCP Server (MissingMCP)",
                "url": f"{config.public_url}/{p}",
                "applicationCategory": "UtilitiesApplication",
                "operatingSystem": "Web",
                "description": landing_desc,
                "offers": {"@type": "Offer", "price": "0"},
            }))

        async def landing(request):
            return HTMLResponse(landing_page)

        async def meta(request):
            return JSONResponse(oauth.metadata(config, adapter))

        async def prmeta(request):
            return JSONResponse(oauth.protected_resource_metadata(config, adapter))

        async def register(request):
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return JSONResponse({"error": "rate_limited"}, status_code=429)
            return await oauth.register_client(request, conn, adapter)

        async def authz_get(request):
            # Throttle per IP like register/token: authz_get mutates process-local
            # state (csrf.issue / put_mfa) on every call. Shares the oauth: bucket.
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
            return await oauth.authorize_get(request, adapter, auth_state, conn, config)

        async def authz_post(request):
            if not rate.check(f"login:{request.client.host}", 5, 60):
                return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
            return await oauth.authorize_post(request, adapter, auth_state, conn, config)

        async def callback(request):
            if not rate.check(f"login:{request.client.host}", 5, 60):
                return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
            return await oauth.authorize_callback(request, adapter, auth_state, conn, config)

        async def token(request):
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return JSONResponse({"error": "rate_limited"}, status_code=429)
            return await oauth.token_exchange(request, conn, config)

        def mcp(method):
            async def handler(request):
                return await proxy.handle_mcp(request, method, adapter, conn, manager,
                                              config, config.gateway_secret, rate)
            return handler

        routes = [
            Route(f"/{p}", landing, methods=["GET"]),
            Route(f"/.well-known/oauth-authorization-server/{p}", meta, methods=["GET"]),
            Route(f"/.well-known/oauth-protected-resource/{p}/mcp", prmeta, methods=["GET"]),
            Route(f"/{p}/oauth/register", register, methods=["POST"]),
            Route(f"/{p}/oauth/authorize", authz_get, methods=["GET"]),
        ]
        if is_upstream_oauth(adapter):
            # login happens at the provider: callback instead of a form POST
            routes.append(Route(f"/{p}/oauth/callback", callback, methods=["GET"]))
        else:
            routes.append(Route(f"/{p}/oauth/authorize", authz_post, methods=["POST"]))
        routes += [
            Route(f"/{p}/oauth/token", token, methods=["POST"]),
            Route(f"/{p}/mcp", mcp("POST"), methods=["POST"]),
            Route(f"/{p}/mcp", mcp("GET"), methods=["GET"]),
            Route(f"/{p}/mcp", mcp("DELETE"), methods=["DELETE"]),
        ]
        return routes

    @contextlib.asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def loop():
            last_stats = None
            while not stop.is_set():
                if bk.enabled and bk.due():
                    # bk.run never raises; to_thread keeps the loop responsive
                    await asyncio.to_thread(bk.run)
                with contextlib.suppress(Exception):
                    for manager in managers.values():
                        await manager.reap_idle()
                    store.cleanup_expired_codes(conn)
                    store.cleanup_expired_tokens(conn)
                    _run_data_cleanup(conn, config.orphan_client_ttl, RETIRED_ADAPTERS)
                    rate.gc()
                    for manager in managers.values():
                        manager.write_snapshot()  # refresh idle_seconds periodically
                    stats = {**store.stats_counts(conn),
                             "active_workers": sum(m.active_count()
                                                   for m in managers.values())}
                    if stats != last_stats:  # log only when something changed
                        log("stats", **stats)
                        last_stats = stats
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=60)

        task = asyncio.create_task(loop())
        log("gateway-started", port=config.port)
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for manager in managers.values():
                manager.shutdown()

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/privacy", privacy, methods=["GET"]),
        Route("/subscribe", subscribe, methods=["POST"]),
        Route("/suggest", suggest, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/favicon.svg", favicon, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/robots.txt", _text(robots_txt, "text/plain"), methods=["GET"]),
        Route("/sitemap.xml", _text(sitemap_xml, "application/xml"), methods=["GET"]),
        Route("/llms.txt", _text(llms_txt, "text/plain"), methods=["GET"]),
        Route("/.well-known/glama.json", _text(glama_json, "application/json"), methods=["GET"]),
        Route("/static/site.js", _text(site_js, "application/javascript"), methods=["GET"]),
        Route("/static/icon.png", static_png("icon.png"), methods=["GET"]),
        Route("/static/favicon-32.png", static_png("favicon-32.png"), methods=["GET"]),
        Route("/static/apple-touch-icon.png", static_png("apple-touch-icon.png"), methods=["GET"]),
    ]
    for a in adapters.values():
        routes.extend(adapter_routes(a))
    # Catch-all (must stay last): unknown GET paths get the home page.
    routes.append(Route("/{path:path}", notfound, methods=["GET"]))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader for running outside Docker (compose uses env_file).
    Real environment variables take precedence over .env values."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ.setdefault(key, val)


def main() -> None:
    import uvicorn
    from .log import setup_logging, resolve_log_level
    _load_dotenv()
    setup_logging()
    config = load_config()
    # access_log off: we emit our own structured mcp-request events; uvicorn's
    # per-request "POST /mcp 200" lines would just duplicate them.
    # log_config=None: don't let uvicorn install its own STDERR handlers — its
    # records propagate to the root logger, where log.py's _StructuredHandler
    # turns them into JSON on stdout (Railway reads plain stderr as error).
    # proxy_headers + forwarded_allow_ips: the container is only reachable via
    # the TLS edge (Railway), so without trusting X-Forwarded-For every request's
    # client.host is the shared edge IP and all per-IP rate limits collapse into
    # one global bucket. Default "*" makes uvicorn take the LEFTMOST X-Forwarded-For
    # entry — which on Railway is the real client IP and is NOT client-spoofable:
    # Railway's edge controls that leftmost position ("clients can send a spoofed
    # X-Forwarded-For, but the real client IP will always be the leftmost entry
    # since our edge proxy appends to the chain" — Railway support). SELF-HOST
    # CAVEAT: behind a proxy that appends to (rather than controls) a client's
    # X-Forwarded-For, "*" + leftmost is spoofable — set FORWARDED_ALLOW_IPS to
    # your proxy's IP(s) so uvicorn walks the trusted hops from the right instead.
    uvicorn.run(build_app(config), host="0.0.0.0", port=config.port,
                log_level=resolve_log_level(), access_log=False, log_config=None,
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"))
