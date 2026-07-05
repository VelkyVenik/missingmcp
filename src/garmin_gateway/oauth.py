from __future__ import annotations
import hmac
import html
import json
import time
from pathlib import Path
from urllib.parse import urlencode
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from . import store, security
from .adapters.garmin import login as garmin_login
from .log import log, log_error, log_exc

_TPL_DIR = Path(__file__).parent / "templates"


def metadata(config) -> dict:
    base = config.public_url
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


async def register_client(request, conn) -> JSONResponse:
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
    store.create_client(conn, client_id, store.hash_token(client_secret), uris, data.get("client_name"))
    return JSONResponse(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "client_secret_post",
        },
        status_code=201,
    )


def _tpl(name: str) -> str:
    return (_TPL_DIR / name).read_text()


class AuthState:
    def __init__(self, csrf):
        self.csrf = csrf
        self._mfa: dict[str, tuple] = {}   # login_id -> (pending, oauth_params, ts)

    def put_mfa(self, pending, oauth_params: dict) -> str:
        self._gc()
        lid = security.new_secret(18)
        self._mfa[lid] = (pending, oauth_params, time.monotonic())
        return lid

    def pop_mfa(self, login_id: str):
        self._gc()
        item = self._mfa.pop(login_id, None)
        if item is None:
            return None
        pending, params, _ts = item
        return pending, params

    def _gc(self) -> None:
        now = time.monotonic()
        for k, (_p, _q, ts) in list(self._mfa.items()):
            if now - ts > 300:
                self._mfa.pop(k, None)


def _fill(template: str, mapping: dict, error: str = "") -> str:
    out = template.replace("{ERROR}", f'<p class="err">{html.escape(error)}</p>' if error else "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", html.escape(v, quote=True))
    return out


def _operator_fields(config) -> dict:
    return {
        "OPERATOR_NAME": config.operator_name,
        "OPERATOR_EMAIL": f" ({config.operator_email})" if config.operator_email else "",
    }


def _login_error_message(reason: str) -> str:
    if reason == "blocked":
        # Garmin (via Cloudflare) rate-limits fresh logins on the mobile SSO
        # endpoint — per-account, not per-IP (garth#217, garminconnect#344) — and
        # the widget/portal fallback can flake. Not the user's fault; a retry usually works.
        return ("Garmin is temporarily rate-limiting new sign-ins (a limit on "
                "Garmin's side, not your password). Please wait a couple of minutes and try again.")
    if reason == "auth":
        return "Garmin sign-in failed — check your Garmin email and password."
    return "Garmin sign-in failed, please try again."


def render_authorize(params: dict, csrf_token: str, config, error: str = "") -> HTMLResponse:
    body = _fill(_tpl("authorize.html"), {
        "CSRF": csrf_token,
        "CLIENT_ID": params.get("client_id", ""),
        "REDIRECT_URI": params.get("redirect_uri", ""),
        "STATE": params.get("state", ""),
        "CODE_CHALLENGE": params.get("code_challenge", ""),
        "METHOD": params.get("code_challenge_method", ""),
        **_operator_fields(config),
    }, error)
    return HTMLResponse(body)


def _oauth_params_from(source) -> dict:
    return {
        "client_id": source.get("client_id", ""),
        "redirect_uri": source.get("redirect_uri", ""),
        "state": source.get("state", ""),
        "code_challenge": source.get("code_challenge", ""),
        "code_challenge_method": source.get("code_challenge_method", ""),
    }


async def authorize_get(request, _templates, state, conn, config) -> HTMLResponse:
    params = _oauth_params_from(request.query_params)
    client = store.get_client(conn, params["client_id"])
    if client is None:
        return HTMLResponse("unknown client_id", status_code=400)
    if not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid redirect_uri", status_code=400)
    if params["code_challenge_method"] != "S256" or not params["code_challenge"]:
        return HTMLResponse("PKCE S256 required", status_code=400)
    return render_authorize(params, state.csrf.issue(), config)


def _finish(conn, config, params: dict, tokens_json: str, email: str) -> RedirectResponse:
    # tokens already verified by the caller (verify_tokens) before we persist
    key = email.strip().lower()
    store.upsert_account(conn, key, tokens_json, config.gateway_secret)
    code = security.new_secret(32)
    store.create_code(
        conn, store.hash_token(code), params["client_id"], params["redirect_uri"],
        params["code_challenge"], params["code_challenge_method"], key,
    )
    sep = "&" if "?" in params["redirect_uri"] else "?"
    location = params["redirect_uri"] + sep + urlencode({"code": code, "state": params["state"]})
    return RedirectResponse(location, status_code=302)


async def authorize_post(request, _templates, state, conn, config) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    has_login_id = bool(form.get("login_id"))
    log("authorize-post", step="mfa" if has_login_id else "login",
        has_csrf=bool(form.get("csrf")), client_id=form.get("client_id", ""))
    if not state.csrf.consume(form.get("csrf", "")):
        log_error("authorize-csrf-invalid", step="mfa" if has_login_id else "login")
        return HTMLResponse("invalid or expired CSRF token", status_code=400)

    # MFA step
    if has_login_id:
        popped = state.pop_mfa(form["login_id"])
        if popped is None:
            log_error("mfa-session-missing", login_id=form.get("login_id", "")[:6])
            return HTMLResponse("MFA session expired, please start over", status_code=400)
        pending, params = popped
        client = store.get_client(conn, params["client_id"])
        if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
            return HTMLResponse("invalid client/redirect_uri", status_code=400)
        try:
            log("mfa-resume-start", mfa_len=len(form.get("mfa_code", "")))
            tokens = garmin_login.resume_login(pending, form.get("mfa_code", ""))
            log("mfa-resume-ok", tokens_len=len(tokens or ""))
        except Exception as e:  # noqa: BLE001 - wrong/expired MFA code: re-prompt
            log_exc("mfa-resume-failed", e, error_type=type(e).__name__, error=str(e))
            lid = state.put_mfa(pending, params)
            body = _fill(_tpl("mfa.html"),
                         {"CSRF": state.csrf.issue(), "LOGIN_ID": lid, **_operator_fields(config)},
                         "Incorrect or expired code, try again")
            return HTMLResponse(body, status_code=400)
        try:
            name = garmin_login.verify_tokens(tokens)
            log("mfa-verify-ok", name=name)
        except garmin_login.GarminLoginError as e:  # tokens didn't authenticate: start over
            log_exc("mfa-verify-failed", e, error=str(e))
            return render_authorize(params, state.csrf.issue(), config, "Garmin sign-in could not be verified")
        log("authorize-finish", step="mfa")
        return _finish(conn, config, params, tokens, params["_email"])

    # login step
    params = _oauth_params_from(form)
    client = store.get_client(conn, params["client_id"])
    if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid client/redirect_uri", status_code=400)
    email = form.get("garmin_email", "")
    password = form.get("garmin_password", "")
    try:
        log("login-start", email=email)
        result = garmin_login.start_login(email, password)
        log("login-start-result", status=result.status)
    except garmin_login.GarminLoginError as e:
        del password
        reason = getattr(e, "reason", "unknown")
        log_exc("login-start-failed", e, reason=reason, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, _login_error_message(reason))
    except Exception as e:  # noqa: BLE001 - unexpected failure
        del password
        log_exc("login-start-failed", e, reason="unknown", error_type=type(e).__name__, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, _login_error_message("unknown"))
    del password  # discard immediately
    if result.status == "needs_mfa":
        params = {**params, "_email": email}
        lid = state.put_mfa(result.pending, params)
        body = _fill(_tpl("mfa.html"),
                     {"CSRF": state.csrf.issue(), "LOGIN_ID": lid, **_operator_fields(config)}, "")
        return HTMLResponse(body)
    try:
        name = garmin_login.verify_tokens(result.tokens_json)
        log("login-verify-ok", name=name)
    except garmin_login.GarminLoginError as e:
        log_exc("login-verify-failed", e, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, "Garmin sign-in could not be verified")
    log("authorize-finish", step="login")
    return _finish(conn, config, params, result.tokens_json, email)


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
    store.create_access_token(conn, store.hash_token(token), row["garmin_user_key"],
                              form.get("client_id"), ttl=config.access_token_ttl)
    log("token-issued", garmin_user_key=row["garmin_user_key"])
    resp = {"access_token": token, "token_type": "Bearer"}
    if config.access_token_ttl:
        resp["expires_in"] = config.access_token_ttl
    return JSONResponse(resp)
