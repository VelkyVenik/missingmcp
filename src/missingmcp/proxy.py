from __future__ import annotations
import json
import time
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from . import store, security
from .adapters.base import is_remote, is_local, SessionExpired
from .workers import WorkerStartError
from .log import log, log_error, log_exc

# Upstream forward timeout for both strategies (parity with the TS proxy's 30s).
FORWARD_TIMEOUT_S = 30.0


def _mcp_tool(body) -> "str | None":
    """Extract the tool/method name from an MCP JSON-RPC request body for usage
    metrics — a tools/call name (e.g. get_activities) or the method (initialize,
    tools/list, ...). Returns None for empty/unparseable/batch bodies. Never
    inspects request arguments or data."""
    if not body:
        return None
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    method = d.get("method")
    if not isinstance(method, str):
        return None
    if method == "tools/call":
        name = (d.get("params") or {}).get("name")
        return name if isinstance(name, str) else "tools/call"
    return method


async def authenticate(request, adapter_name, conn, rate) -> "str | Response":
    ip = request.client.host if request.client else "unknown"
    if not rate.check(f"unauth:{ip}", limit=30, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token_hash = store.hash_token(header[7:])
    if not rate.check(f"tok:{token_hash}", limit=60, window=60):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    found = store.account_key_for_token_hash(conn, token_hash)
    if found is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    tok_adapter, account_key = found
    if tok_adapter != adapter_name:                 # token belongs to another connector
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    return account_key


def _session_expired(adapter) -> JSONResponse:
    """Stored credentials went stale: worker start failed (strategy B) or the
    remote upstream rejected the injected credentials (strategy A)."""
    return JSONResponse(
        {"error": f"{adapter.name}_session_expired",
         "message": f"Your {adapter.display_name} session expired. "
                    f"Please reconnect the {adapter.display_name} MCP server."},
        status_code=502,
    )


async def handle_mcp(request, method, adapter, conn, manager, config, secret, rate) -> Response:
    t0 = time.monotonic()
    log("mcp-request", adapter=adapter.name, method=method,
        has_session=bool(request.headers.get("mcp-session-id")))
    auth = await authenticate(request, adapter.name, conn, rate)
    if isinstance(auth, Response):
        log("mcp-auth-rejected", method=method, status=auth.status_code)
        return auth
    key = auth

    if is_local(adapter.forward) and method != "POST":
        # stateless in-process server: no SSE listen stream, no sessions
        return JSONResponse({"error": "method_not_allowed"}, status_code=405)

    body = await security.read_body_limited(request)
    if body is None:
        return JSONResponse({"error": "request_too_large"}, status_code=413)

    tokens = store.get_account_tokens(conn, adapter.name, key, secret)
    if tokens is None:
        return JSONResponse({"error": "unknown_account"}, status_code=401)

    tool = _mcp_tool(body)
    if tool:
        try:
            store.record_usage(conn, adapter.name, key, tool)
        except Exception:  # noqa: BLE001 - usage metrics must never break a request
            pass

    if is_local(adapter.forward):
        try:
            status, headers, payload = await adapter.forward.handle(conn, key, tokens, body)
        except SessionExpired:
            log_error("local-forward-auth-stale", adapter=adapter.name, account=key)
            return _session_expired(adapter)
        ms = int((time.monotonic() - t0) * 1000)
        log("mcp-response", adapter=adapter.name, account=key, tool=tool,
            status=status, ttfb_ms=ms, total_ms=ms, bytes=len(payload))
        return Response(payload, status_code=status, headers=headers)

    # --- strategy dispatch: where the upstream is and what extra headers it needs
    remote = is_remote(adapter.forward)
    if remote:
        url = adapter.forward.upstream_url
        extra_headers = adapter.forward.headers(tokens)
    else:
        try:
            log("worker-ensure-start", account=key)
            port = await manager.ensure_worker(key, tokens)
            log("worker-ensure-ok", port=port, account=key,
                ms=int((time.monotonic() - t0) * 1000))
        except WorkerStartError as e:
            log_exc("worker-start-failed", e, error=str(e), account=key)
            return _session_expired(adapter)
        url = f"http://127.0.0.1:{port}/mcp"
        extra_headers = {}

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
    upstream_headers.update(extra_headers)

    client = httpx.AsyncClient(timeout=httpx.Timeout(FORWARD_TIMEOUT_S))
    if not remote:
        # Mark the worker busy so reap_idle / _enforce_cap won't kill it mid-stream.
        # Paired with finish() on every exit path below.
        manager.request_started(key)
    finish = (lambda: None) if remote else (lambda: manager.request_finished(key))
    try:
        req = client.build_request(method, url, headers=upstream_headers,
                                   content=body if method != "GET" else None)
        upstream = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        finish()
        log_error("mcp-timeout", adapter=adapter.name, account=key, tool=tool,
                  ms=int((time.monotonic() - t0) * 1000))
        return JSONResponse({"error": "gateway_timeout"}, status_code=504)
    except httpx.HTTPError as e:
        await client.aclose()
        finish()
        log_error("mcp-forward-error", error=type(e).__name__,
                  adapter=adapter.name, account=key, tool=tool,
                  ms=int((time.monotonic() - t0) * 1000))
        return JSONResponse({"error": "bad_gateway"}, status_code=502)
    ttfb_ms = int((time.monotonic() - t0) * 1000)   # request in → upstream headers out

    if remote and upstream.status_code in (401, 403):
        # The shared upstream rejected the injected credentials — don't stream
        # the raw 401/403 through; surface it like a worker start failure.
        log_error("remote-forward-auth-stale", adapter=adapter.name,
                  status=upstream.status_code, account=key)
        await upstream.aclose()
        await client.aclose()
        return _session_expired(adapter)

    resp_headers = {}
    ct = upstream.headers.get("content-type")
    if ct:
        resp_headers["Content-Type"] = ct
    up_sid = upstream.headers.get("mcp-session-id")
    if up_sid:
        resp_headers["Mcp-Session-Id"] = up_sid

    async def stream():
        sent = 0
        try:
            async for chunk in upstream.aiter_raw():
                sent += len(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            finish()
            # The per-request latency record: ttfb_ms = gateway overhead +
            # upstream time to headers, total_ms includes streaming the body.
            log("mcp-response", adapter=adapter.name, account=key, tool=tool,
                status=upstream.status_code, ttfb_ms=ttfb_ms,
                total_ms=int((time.monotonic() - t0) * 1000), bytes=sent)

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=resp_headers)
