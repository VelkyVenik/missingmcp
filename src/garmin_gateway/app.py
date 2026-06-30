from __future__ import annotations
import asyncio
import contextlib
import os
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware
from . import store, oauth, proxy, security
from .config import load_config, Config
from .workers import WorkerManager
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
    manager = WorkerManager(config)
    auth_state = oauth.AuthState(security.CsrfStore())
    rate = security.RateLimiter()

    landing = (_TPL / "landing.html").read_text().replace(
        "{PUBLIC_URL}", config.public_url
    ).replace("{OPERATOR_NAME}", config.operator_name).replace(
        "{OPERATOR_EMAIL}", f" ({config.operator_email})" if config.operator_email else ""
    )

    async def home(request):
        return HTMLResponse(landing)

    async def notfound(request):
        # Catch-all for unknown GET paths: show the instructional landing page
        # (humans see how to connect) but with a 404 so API/discovery clients
        # still read it as "not here".
        return HTMLResponse(landing, status_code=404)

    async def healthz(request):
        return PlainTextResponse("ok")

    async def meta(request):
        return JSONResponse(oauth.metadata(config))

    async def register(request):
        if not rate.check(f"oauth:{request.client.host}", 20, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await oauth.register_client(request, conn)

    async def authz_get(request):
        return await oauth.authorize_get(request, None, auth_state, conn, config)

    async def authz_post(request):
        if not rate.check(f"login:{request.client.host}", 5, 60):
            return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
        return await oauth.authorize_post(request, None, auth_state, conn, config)

    async def token(request):
        if not rate.check(f"oauth:{request.client.host}", 20, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await oauth.token_exchange(request, conn)

    def mcp(method):
        async def handler(request):
            return await proxy.handle_mcp(request, method, conn, manager, config, config.gateway_secret, rate)
        return handler

    @contextlib.asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def loop():
            last_stats = None
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    await manager.reap_idle()
                    store.cleanup_expired_codes(conn)
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
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", meta, methods=["GET"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authz_get, methods=["GET"]),
        Route("/oauth/authorize", authz_post, methods=["POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/mcp", mcp("POST"), methods=["POST"]),
        Route("/mcp", mcp("GET"), methods=["GET"]),
        Route("/mcp", mcp("DELETE"), methods=["DELETE"]),
        # Catch-all (must stay last): unknown GET paths get the landing page.
        Route("/{path:path}", notfound, methods=["GET"]),
    ]
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
