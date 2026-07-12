from __future__ import annotations
import asyncio
import hmac
import html
import json
import time
from urllib.parse import urlencode
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from . import pages, store, security
from .adapters.base import LoginError, SecondFactorError, SecondFactorNeeded, is_upstream_oauth
from .log import log, log_warn, log_error, log_exc


def metadata(config, adapter) -> dict:
    base = f"{config.public_url}/{adapter.name}"
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


def protected_resource_metadata(config, adapter) -> dict:
    # RFC 9728: points the MCP client at this resource's authorization server.
    base = config.public_url
    return {
        "resource": f"{base}/{adapter.name}/mcp",
        "authorization_servers": [f"{base}/{adapter.name}"],
    }


async def register_client(request, conn, adapter) -> JSONResponse:
    body = await security.read_body_limited(request)
    if body is None:
        return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    client_id = security.new_secret(16)
    client_secret = security.new_secret(32)
    store.create_client(conn, client_id, store.hash_token(client_secret), uris,
                        data.get("client_name"), adapter.name)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_secret": client_secret,
            # RFC 7591 requires this (as a number) whenever a client_secret is
            # issued; 0 = never expires. Strict clients (oauth4webapi) reject
            # the registration response without it.
            "client_secret_expires_at": 0,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "client_secret_post",
        },
        status_code=201,
    )


def _authorize_page(adapter, config) -> str:
    # {OPERATOR} is trusted HTML (escaped inside operator_html) — it must be
    # replaced into the raw template, never through _fill's escaping pass.
    # noindex: sign-in forms must not land in search results.
    return pages.render_page(
        adapter.authorize_template, f"Connect {adapter.display_name} — MissingMCP",
        noindex=True,
    ).replace("{OPERATOR}", pages.operator_html(config))


def _second_factor_page(adapter, config) -> str:
    return pages.render_page(
        adapter.second_factor_template, f"{adapter.display_name} verification — MissingMCP",
        noindex=True,
    ).replace("{OPERATOR}", pages.operator_html(config))


class AuthState:
    def __init__(self, csrf):
        self.csrf = csrf
        self._mfa: dict[str, tuple] = {}   # login_id -> (pending, oauth_params, adapter, ts)

    def put_mfa(self, pending, oauth_params: dict, adapter_name: str) -> str:
        self._gc()
        lid = security.new_secret(18)
        self._mfa[lid] = (pending, oauth_params, adapter_name, time.monotonic())
        return lid

    def pop_mfa(self, login_id: str, adapter_name: str):
        # AuthState is shared by all adapter routes: a login_id minted by one
        # adapter's flow must read as expired on another's (state is consumed
        # either way — same as a replayed/expired id).
        self._gc()
        item = self._mfa.pop(login_id, None)
        if item is None:
            return None
        pending, params, owner, _ts = item
        if owner != adapter_name:
            return None
        return pending, params

    def _gc(self) -> None:
        now = time.monotonic()
        for k, (_p, _q, _a, ts) in list(self._mfa.items()):
            if now - ts > 300:
                self._mfa.pop(k, None)


def _fill(template: str, mapping: dict, error: str = "") -> str:
    out = template.replace("{ERROR}", f'<p class="err">{html.escape(error)}</p>' if error else "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", html.escape(v, quote=True))
    return out


def _operator_fields(config) -> dict:
    return {
        "OPERATOR_EMAIL": f" ({config.operator_email})" if config.operator_email else "",
    }


def _oauth_hidden_fields(params: dict, csrf_token: str) -> str:
    """The CSRF + OAuth-param hidden inputs every adapter's authorize form must
    POST back — built once here so per-adapter templates can't drift apart."""
    fields = [
        ("csrf", csrf_token),
        ("client_id", params.get("client_id", "")),
        ("redirect_uri", params.get("redirect_uri", "")),
        ("state", params.get("state", "")),
        ("code_challenge", params.get("code_challenge", "")),
        ("code_challenge_method", params.get("code_challenge_method", "")),
    ]
    return "\n".join(
        f'  <input type="hidden" name="{n}" value="{html.escape(v, quote=True)}">'
        for n, v in fields
    )


def render_authorize(params: dict, csrf_token: str, config, adapter, error: str = "") -> HTMLResponse:
    body = _fill(_authorize_page(adapter, config), {
        "AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize",
        **_operator_fields(config),
    }, error)
    # after _fill: the fragment is HTML (must not be escaped) and its escaped
    # values must not be re-scanned for placeholders
    body = body.replace("{OAUTH_FIELDS}", _oauth_hidden_fields(params, csrf_token))
    return HTMLResponse(body)


def _oauth_params_from(source) -> dict:
    return {
        "client_id": source.get("client_id", ""),
        "redirect_uri": source.get("redirect_uri", ""),
        "state": source.get("state", ""),
        "code_challenge": source.get("code_challenge", ""),
        "code_challenge_method": source.get("code_challenge_method", ""),
    }


async def authorize_get(request, adapter, state, conn, config) -> HTMLResponse | RedirectResponse:
    params = _oauth_params_from(request.query_params)
    client = store.get_client(conn, params["client_id"])
    if client is None:
        return HTMLResponse("unknown client_id", status_code=400)
    if not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid redirect_uri", status_code=400)
    if params["code_challenge_method"] != "S256" or not params["code_challenge"]:
        return HTMLResponse("PKCE S256 required", status_code=400)
    if is_upstream_oauth(adapter):
        # No form of ours: stash Claude's OAuth params (same one-time TTL stash
        # as MFA, pending=None) and send the user to the provider. The stash id
        # rides in the provider's `state` and doubles as callback CSRF.
        sid = state.put_mfa(None, params, adapter.name)
        log("upstream-oauth-start", adapter=adapter.name, client_id=params["client_id"])
        return RedirectResponse(adapter.authorize_redirect_url(sid), status_code=302)
    return render_authorize(params, state.csrf.issue(), config, adapter)


def _finish(conn, config, params: dict, blob: str, adapter_name: str, account_key: str) -> RedirectResponse:
    # blob already verified by the caller (adapter.verify) before we persist
    store.upsert_account(conn, adapter_name, account_key, blob, config.gateway_secret)
    code = security.new_secret(32)
    store.create_code(
        conn, store.hash_token(code), params["client_id"], params["redirect_uri"],
        params["code_challenge"], params["code_challenge_method"], adapter_name, account_key,
    )
    sep = "&" if "?" in params["redirect_uri"] else "?"
    location = params["redirect_uri"] + sep + urlencode({"code": code, "state": params["state"]})
    return RedirectResponse(location, status_code=302)


async def _bounded(config, fn, *args):
    """Run a blocking adapter sign-in step (garminconnect does synchronous network
    I/O) off the event loop, capped at config.login_timeout. Without to_thread the
    call would freeze the single-node event loop for every user for the duration;
    without the cap a Garmin sign-in that Garmin is rate-limiting can block ~2
    minutes (observed: a 125s authorize POST before the client gave up → 499).
    Raises TimeoutError past the deadline (the abandoned thread finishes on its own)."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), config.login_timeout)


def _timeout_message(adapter) -> str:
    return (f"{adapter.display_name} sign-in timed out — the service may be "
            "rate-limiting new sign-ins. Please wait a moment and try again.")


async def authorize_post(request, adapter, state, conn, config) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    has_login_id = bool(form.get("login_id"))
    log("authorize-post", step="mfa" if has_login_id else "login",
        has_csrf=bool(form.get("csrf")), client_id=form.get("client_id", ""))
    if not state.csrf.consume(form.get("csrf", "")):
        log_error("authorize-csrf-invalid", step="mfa" if has_login_id else "login")
        return HTMLResponse("invalid or expired CSRF token", status_code=400)

    # second-factor step (Garmin: MFA)
    if has_login_id:
        popped = state.pop_mfa(form["login_id"], adapter.name)
        if popped is None:
            log_error("mfa-session-missing", login_id=form.get("login_id", "")[:6])
            return HTMLResponse("MFA session expired, please start over", status_code=400)
        pending, params = popped
        client = store.get_client(conn, params["client_id"])
        if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
            return HTMLResponse("invalid client/redirect_uri", status_code=400)
        try:
            log("mfa-resume-start", mfa_len=len(form.get("mfa_code", "")))
            t0 = time.monotonic()
            result = await _bounded(config, adapter.resume_second_factor, pending, form)
            log("mfa-resume-ok", tokens_len=len(result.blob or ""))
        except TimeoutError:  # resume hung (rate-limited upstream): start over
            log_warn("mfa-resume-timeout", ms=int((time.monotonic() - t0) * 1000),
                     timeout=config.login_timeout)
            return render_authorize(params, state.csrf.issue(), config, adapter,
                                    _timeout_message(adapter))
        except SecondFactorError as e:  # wrong/expired code: re-prompt
            log_exc("mfa-resume-failed", e, error_type=type(e).__name__, error=str(e))
            lid = state.put_mfa(e.state, params, adapter.name)
            body = _fill(_second_factor_page(adapter, config),
                         {"CSRF": state.csrf.issue(), "LOGIN_ID": lid,
                          "AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize",
                          **_operator_fields(config)},
                         str(e))
            return HTMLResponse(body, status_code=400)
        except LoginError as e:  # contract: "start over" — back to the credential form
            log_exc("mfa-resume-fatal", e, error=str(e))
            return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
        try:
            t0 = time.monotonic()
            name = await _bounded(config, adapter.verify, result.blob)
            log("mfa-verify-ok", name=name, ms=int((time.monotonic() - t0) * 1000))
        except TimeoutError:  # verification hung: start over
            log_warn("mfa-verify-timeout", ms=int((time.monotonic() - t0) * 1000),
                     timeout=config.login_timeout)
            return render_authorize(params, state.csrf.issue(), config, adapter,
                                    _timeout_message(adapter))
        except LoginError as e:  # blob didn't authenticate: start over
            log_exc("mfa-verify-failed", e, error=str(e))
            return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
        log("authorize-finish", step="mfa")
        return _finish(conn, config, params, result.blob, adapter.name, result.account_key)

    # login step
    params = _oauth_params_from(form)
    client = store.get_client(conn, params["client_id"])
    if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid client/redirect_uri", status_code=400)
    try:
        log("login-start", email=adapter.login_hint(form))
        t0 = time.monotonic()
        result = await _bounded(config, adapter.start_login, form)
        log("login-start-result",
            status="needs_mfa" if isinstance(result, SecondFactorNeeded) else "ok",
            ms=int((time.monotonic() - t0) * 1000))
    except TimeoutError:  # sign-in hung (rate-limited upstream): let the user retry fast
        log_warn("login-start-timeout", ms=int((time.monotonic() - t0) * 1000),
                 timeout=config.login_timeout)
        return render_authorize(params, state.csrf.issue(), config, adapter,
                                _timeout_message(adapter))
    except LoginError as e:
        log_exc("login-start-failed", e, reason=e.reason, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
    except Exception as e:  # noqa: BLE001 - unexpected failure
        log_exc("login-start-failed", e, reason="unknown", error_type=type(e).__name__, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, adapter,
                                f"{adapter.display_name} sign-in failed, please try again.")
    if isinstance(result, SecondFactorNeeded):
        lid = state.put_mfa(result.state, params, adapter.name)
        body = _fill(_second_factor_page(adapter, config),
                     {"CSRF": state.csrf.issue(), "LOGIN_ID": lid,
                      "AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize",
                      **_operator_fields(config)}, "")
        return HTMLResponse(body)
    try:
        t0 = time.monotonic()
        name = await _bounded(config, adapter.verify, result.blob)
        log("login-verify-ok", name=name, ms=int((time.monotonic() - t0) * 1000))
    except TimeoutError:  # verification hung: let the user retry fast
        log_warn("login-verify-timeout", ms=int((time.monotonic() - t0) * 1000),
                 timeout=config.login_timeout)
        return render_authorize(params, state.csrf.issue(), config, adapter,
                                _timeout_message(adapter))
    except LoginError as e:
        log_exc("login-verify-failed", e, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
    log("authorize-finish", step="login")
    return _finish(conn, config, params, result.blob, adapter.name, result.account_key)


async def token_exchange(request, conn, config) -> JSONResponse:
    form = await request.form()
    log("token-exchange", grant_type=form.get("grant_type", ""),
        client_id=form.get("client_id", ""), redirect_uri=form.get("redirect_uri", ""))
    if form.get("grant_type") != "authorization_code":
        log_error("token-bad-grant", grant_type=form.get("grant_type", ""))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    client = store.get_client(conn, form.get("client_id", ""))
    if client is None or not hmac.compare_digest(
        store.hash_token(form.get("client_secret", "")), client["client_secret_hash"]
    ):
        log_error("token-invalid-client", client_known=client is not None)
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    row = store.consume_code(conn, store.hash_token(form.get("code", "")))
    if row is None:
        log_error("token-invalid-grant", reason="code_not_found_or_expired")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if row["client_id"] != form.get("client_id") or row["redirect_uri"] != form.get("redirect_uri"):
        log_error("token-invalid-grant", reason="client_or_redirect_mismatch")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not security.verify_pkce(form.get("code_verifier", ""), row["code_challenge"], row["code_challenge_method"]):
        log_error("token-invalid-grant", reason="pkce_mismatch")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    token = security.new_secret(32)
    store.create_access_token(conn, store.hash_token(token), row["adapter"], row["account_key"],
                              form.get("client_id"), ttl=config.access_token_ttl)
    log("token-issued", account_key=row["account_key"])
    resp = {"access_token": token, "token_type": "Bearer"}
    if config.access_token_ttl:
        resp["expires_in"] = config.access_token_ttl
    return JSONResponse(resp)


def _upstream_error(config, adapter, message: str) -> HTMLResponse:
    # noindex like the sign-in forms; {OPERATOR} is trusted HTML, replaced
    # after _fill's escaping pass (same rule as _authorize_page).
    body = _fill(pages.render_page("upstream_error.html",
                                   f"Connect {adapter.display_name} — MissingMCP",
                                   noindex=True),
                 {"DISPLAY_NAME": adapter.display_name, **_operator_fields(config)},
                 message
                 ).replace("{OPERATOR}", pages.operator_html(config))
    return HTMLResponse(body, status_code=400)


async def authorize_callback(request, adapter, state, conn, config) -> HTMLResponse | RedirectResponse:
    """The provider's redirect back (login shape C). Pop-once state lookup,
    re-validate the DCR client, exchange the code via the adapter, then the
    standard verify-then-persist finish."""
    q = request.query_params
    popped = state.pop_mfa(q.get("state", ""), adapter.name)
    if popped is None:
        log_warn("upstream-oauth-callback", adapter=adapter.name, status="expired")
        return _upstream_error(config, adapter,
                               "This sign-in link expired — go back to Claude and try connecting again.")
    _pending, params = popped
    client = store.get_client(conn, params["client_id"])
    if client is None or not security.validate_redirect_uri(params["redirect_uri"],
                                                            client["redirect_uris"]):
        return HTMLResponse("invalid client/redirect_uri", status_code=400)
    if q.get("error") or not q.get("code"):
        # the user declined at the provider — expected, not an anomaly
        log_warn("upstream-oauth-callback", adapter=adapter.name, status="denied",
                 reason=q.get("error", "no_code"))
        return _upstream_error(config, adapter,
                               f"{adapter.display_name} declined the connection — go back to Claude and try again.")
    try:
        result = await adapter.handle_callback(q)
        # verify is a blocking network step (WhoopAdapter.verify does a 15s
        # httpx.get) — bound it off the loop exactly like the login/MFA paths,
        # so a rate-limiting provider can't freeze the single-node event loop.
        t0 = time.monotonic()
        name = await _bounded(config, adapter.verify, result.blob)
        log("upstream-verify-ok", name=name, ms=int((time.monotonic() - t0) * 1000))
    except TimeoutError:  # verify hung (rate-limited provider): send the user back to retry
        log_warn("upstream-verify-timeout", adapter=adapter.name, timeout=config.login_timeout)
        return _upstream_error(config, adapter, _timeout_message(adapter))
    except LoginError as e:
        log_exc("upstream-oauth-callback", e, adapter=adapter.name, status="error",
                error=str(e))
        return _upstream_error(config, adapter, str(e))
    except Exception as e:  # noqa: BLE001 - unexpected failure (network, provider bug)
        log_exc("upstream-oauth-callback", e, adapter=adapter.name, status="error",
                error_type=type(e).__name__, error=str(e))
        return _upstream_error(config, adapter,
                               f"{adapter.display_name} sign-in failed, please try again.")
    log("upstream-oauth-callback", adapter=adapter.name, status="ok")
    log("authorize-finish", step="upstream")
    return _finish(conn, config, params, result.blob, adapter.name, result.account_key)
