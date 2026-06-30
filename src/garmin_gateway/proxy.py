from __future__ import annotations
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from . import store, security
from .workers import WorkerStartError
from .log import log_error


async def authenticate(request, conn, rate) -> "str | Response":
    ip = request.client.host if request.client else "unknown"
    if not rate.check(f"unauth:{ip}", limit=30, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token_hash = store.hash_token(header[7:])
    if not rate.check(f"tok:{token_hash}", limit=60, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    key = store.account_key_for_token_hash(conn, token_hash)
    if key is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    return key


async def handle_mcp(request, method, conn, manager, config, secret, rate) -> Response:
    auth = await authenticate(request, conn, rate)
    if isinstance(auth, Response):
        return auth
    key = auth

    body = await security.read_body_limited(request)
    if body is None:
        return JSONResponse({"error": "request_too_large"}, status_code=413)

    tokens = store.get_account_tokens(conn, key, secret)
    if tokens is None:
        return JSONResponse({"error": "unknown_account"}, status_code=401)

    try:
        port = await manager.ensure_worker(key, tokens)
    except WorkerStartError:
        return JSONResponse(
            {"error": "garmin_session_expired",
             "message": "Your Garmin session expired. Please reconnect the Garmin MCP server."},
            status_code=502,
        )

    upstream_headers = {}
    accept = request.headers.get("accept")
    if accept:
        upstream_headers["Accept"] = accept
    sid = request.headers.get("mcp-session-id")
    if sid:
        if not security.validate_session_id(sid):
            return JSONResponse({"error": "invalid_session_id"}, status_code=400)
        upstream_headers["Mcp-Session-Id"] = sid
    if method != "DELETE":
        upstream_headers["Content-Type"] = "application/json"

    url = f"http://127.0.0.1:{port}/mcp"
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    try:
        req = client.build_request(method, url, headers=upstream_headers,
                                   content=body if method != "GET" else None)
        upstream = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return JSONResponse({"error": "gateway_timeout"}, status_code=504)
    except httpx.HTTPError as e:
        await client.aclose()
        log_error("mcp-forward-error", error=type(e).__name__)
        return JSONResponse({"error": "bad_gateway"}, status_code=502)

    resp_headers = {}
    ct = upstream.headers.get("content-type")
    if ct:
        resp_headers["Content-Type"] = ct
    up_sid = upstream.headers.get("mcp-session-id")
    if up_sid:
        resp_headers["Mcp-Session-Id"] = up_sid

    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=resp_headers)
