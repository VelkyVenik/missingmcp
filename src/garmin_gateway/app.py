from __future__ import annotations
import asyncio
import contextlib
import os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware
from . import store, oauth, proxy, security
from .config import load_config, Config
from .workers import WorkerManager
from .adapters import build_adapters
from .log import log

_TPL = Path(__file__).parent / "templates"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        for k, v in security.security_headers().items():
            resp.headers.setdefault(k, v)
        return resp


def build_app(config: Config) -> Starlette:
    conn = store.init_db(config.db_path)
    adapters = build_adapters(config)
    garmin = adapters["garmin"]
    # Single worker manager: garmin is the only worker-based adapter today. A
    # second, non-worker adapter (step 4) reworks forwarding and revisits this.
    manager = WorkerManager(config, garmin.forward)
    auth_state = oauth.AuthState(security.CsrfStore())
    rate = security.RateLimiter()

    def _render(name: str) -> str:
        return (_TPL / name).read_text().replace(
            "{PUBLIC_URL}", config.public_url
        ).replace("{OPERATOR_NAME}", config.operator_name).replace(
            "{OPERATOR_EMAIL}", f" ({config.operator_email})" if config.operator_email else ""
        )

    garmin_page = _render("garmin.html")
    home_page = _render("home.html")

    async def home(request):
        return HTMLResponse(home_page)

    async def garmin_landing(request):
        return HTMLResponse(garmin_page)

    async def notfound(request):
        # Catch-all for unknown GET paths: humans get the MissingMCP home
        # (with links to every connector) but with a 404 status so
        # API/discovery clients still read it as "not here".
        return HTMLResponse(home_page, status_code=404)

    favicon_svg = (_TPL / "favicon.svg").read_text()

    async def healthz(request):
        return PlainTextResponse("ok")

    async def favicon(request):
        return Response(favicon_svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    def adapter_routes(adapter):
        p = adapter.name

        async def meta(request):
            return JSONResponse(oauth.metadata(config, adapter))

        async def prmeta(request):
            return JSONResponse(oauth.protected_resource_metadata(config, adapter))

        async def register(request):
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return JSONResponse({"error": "rate_limited"}, status_code=429)
            return await oauth.register_client(request, conn, adapter)

        async def authz_get(request):
            return await oauth.authorize_get(request, adapter, auth_state, conn, config)

        async def authz_post(request):
            if not rate.check(f"login:{request.client.host}", 5, 60):
                return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
            return await oauth.authorize_post(request, adapter, auth_state, conn, config)

        async def token(request):
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return JSONResponse({"error": "rate_limited"}, status_code=429)
            return await oauth.token_exchange(request, conn, config)

        def mcp(method):
            async def handler(request):
                return await proxy.handle_mcp(request, method, adapter, conn, manager,
                                              config, config.gateway_secret, rate)
            return handler

        return [
            Route(f"/.well-known/oauth-authorization-server/{p}", meta, methods=["GET"]),
            Route(f"/.well-known/oauth-protected-resource/{p}/mcp", prmeta, methods=["GET"]),
            Route(f"/{p}/oauth/register", register, methods=["POST"]),
            Route(f"/{p}/oauth/authorize", authz_get, methods=["GET"]),
            Route(f"/{p}/oauth/authorize", authz_post, methods=["POST"]),
            Route(f"/{p}/oauth/token", token, methods=["POST"]),
            Route(f"/{p}/mcp", mcp("POST"), methods=["POST"]),
            Route(f"/{p}/mcp", mcp("GET"), methods=["GET"]),
            Route(f"/{p}/mcp", mcp("DELETE"), methods=["DELETE"]),
        ]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def loop():
            last_stats = None
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    await manager.reap_idle()
                    store.cleanup_expired_codes(conn)
                    store.cleanup_expired_tokens(conn)
                    rate.gc()
                    manager.write_snapshot()  # refresh idle_seconds periodically
                    stats = {**store.stats_counts(conn),
                             "active_workers": manager.active_count()}
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
            manager.shutdown()

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/garmin", garmin_landing, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/favicon.svg", favicon, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
    ]
    for a in adapters.values():
        routes.extend(adapter_routes(a))
    # Catch-all (must stay last): unknown GET paths get the landing page.
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
    uvicorn.run(build_app(config), host="0.0.0.0", port=config.port,
                log_level=resolve_log_level(), access_log=False)
