"""WHOOP v2 HTTP client: upstream OAuth code exchange, gateway-owned token
refresh, authenticated GETs. WHOOP rotates the refresh token on every refresh
(the old pair is invalidated immediately), so refresh is serialized per
account and the rotated blob is persisted to the store before proceeding."""
from __future__ import annotations
import asyncio
import json
import time
from urllib.parse import urlencode
import httpx
from ... import store
from ...log import log, log_warn

REFRESH_MARGIN_S = 120
HTTP_TIMEOUT_S = 15.0
SCOPES = "read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement offline"


class WhoopAuthError(Exception):
    """The account's tokens can't be made valid (refresh rejected, or the API
    keeps answering 401). Callers raise SessionExpired, which the proxy surfaces
    as a re-auth 401 with the RFC 9728 challenge."""


def _blob_from_token_response(tok: dict) -> dict:
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
    }


class WhoopApi:
    def __init__(self, config):
        self._cfg = config
        self._locks: dict[str, asyncio.Lock] = {}

    # --- URLs ---------------------------------------------------------------
    @property
    def redirect_uri(self) -> str:
        return f"{self._cfg.public_url}/whoop/oauth/callback"

    @property
    def _token_url(self) -> str:
        return f"{self._cfg.whoop_api_base}/oauth/oauth2/token"

    def _data_url(self, path: str) -> str:
        return f"{self._cfg.whoop_api_base}/developer{path}"

    @property
    def profile_url(self) -> str:
        return self._data_url("/v2/user/profile/basic")

    def auth_url(self, state_id: str) -> str:
        q = urlencode({
            "response_type": "code",
            "client_id": self._cfg.whoop_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state_id,
        })
        return f"{self._cfg.whoop_api_base}/oauth/oauth2/auth?{q}"

    # --- login-time -----------------------------------------------------------
    async def exchange_code(self, code: str) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            r = await client.post(self._token_url, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._cfg.whoop_client_id,
                "client_secret": self._cfg.whoop_client_secret,
                "redirect_uri": self.redirect_uri,
            })
        if r.status_code != 200:
            raise WhoopAuthError(f"token exchange failed ({r.status_code})")
        return _blob_from_token_response(r.json())

    async def fetch_profile(self, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            r = await client.get(self.profile_url,
                                 headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code != 200:
            raise WhoopAuthError(f"profile fetch failed ({r.status_code})")
        return r.json()

    # --- request-time -----------------------------------------------------------
    async def get(self, conn, account_key: str, blob: dict, path: str,
                  params: dict | None = None) -> "tuple[int, object]":
        """GET a data endpoint with a fresh token: refresh ahead of expiry, one
        forced refresh + retry on an unexpected 401. Returns (status, json).
        Raises WhoopAuthError when the tokens are beyond saving."""
        blob = await self.ensure_fresh(conn, account_key, blob)
        status, payload = await self._get_once(blob, path, params)
        if status == 401:
            blob = await self.ensure_fresh(conn, account_key, blob, force=True)
            status, payload = await self._get_once(blob, path, params)
            if status == 401:
                raise WhoopAuthError("WHOOP rejected a freshly refreshed token")
        return status, payload

    async def _get_once(self, blob: dict, path: str, params: dict | None):
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            r = await client.get(self._data_url(path), params=params or {},
                                 headers={"Authorization": f"Bearer {blob['access_token']}"})
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text}
        return r.status_code, payload

    async def ensure_fresh(self, conn, account_key: str, blob: dict,
                           force: bool = False) -> dict:
        orig_token = blob["access_token"]
        if not force and blob["expires_at"] - time.time() > REFRESH_MARGIN_S:
            return blob
        lock = self._locks.setdefault(account_key, asyncio.Lock())
        async with lock:
            # Re-read: a queued waiter must reuse the blob another request just
            # rotated instead of burning the (now dead) refresh token again.
            current = store.get_account_tokens(conn, "whoop", account_key,
                                               self._cfg.gateway_secret)
            if current is not None:
                blob = json.loads(current)
            if force and blob["access_token"] != orig_token:
                return blob            # someone else already rotated past our stale copy
            if not force and blob["expires_at"] - time.time() > REFRESH_MARGIN_S:
                return blob
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                r = await client.post(self._token_url, data={
                    "grant_type": "refresh_token",
                    "refresh_token": blob["refresh_token"],
                    "client_id": self._cfg.whoop_client_id,
                    "client_secret": self._cfg.whoop_client_secret,
                    "scope": "offline",
                })
            if r.status_code != 200:
                try:
                    err = r.json().get("error", "")
                except ValueError:
                    err = ""
                if err == "invalid_grant":
                    # The member revoked the app at WHOOP (or the rotation was
                    # lost for good). Per the WHOOP API Terms of Use, stored
                    # content is deleted on termination: purge the dead blob
                    # and cut gateway access — reconnecting mints a fresh pair.
                    store.delete_account(conn, "whoop", account_key)
                    store.revoke_account(conn, "whoop", account_key)
                    log_warn("whoop-account-revoked", account=account_key)
                    raise WhoopAuthError("WHOOP access was revoked")
                log_warn("whoop-refresh-failed", account=account_key,
                         status=r.status_code)
                raise WhoopAuthError("WHOOP token refresh failed")
            new = _blob_from_token_response(r.json())
            new["user_id"] = blob.get("user_id")
            new["email"] = blob.get("email")
            # Persist BEFORE using: the old pair is already dead upstream.
            store.upsert_account(conn, "whoop", account_key, json.dumps(new),
                                 self._cfg.gateway_secret)
            log("whoop-refresh-ok", account=account_key)
            return new
