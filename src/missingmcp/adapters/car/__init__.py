from __future__ import annotations
import json
from datetime import datetime, timezone
import time
from typing import Mapping
from ..base import (LoginError, LoginOk, SecondFactorError, SecondFactorNeeded,
                    normalize_account_key)
from . import bmw, skoda
from .common import CarUpstreamError
from .mcp import CarLocalForward, _parse_ts

BRANDS = {"skoda": "Škoda", "bmw": "BMW / Mini"}

# CarUpstreamError.kind → (form message, log reason)
_LOGIN_ERRORS = {
    "invalid_key": ("That API key isn't valid or has expired — create a new one in the "
                    "MyŠkoda app.", "invalid_key"),
    "vin_not_covered": ("This key doesn't cover that VIN — check the VIN, or create the "
                        "key for this car.", "vin_not_covered"),
    "rate_limited": ("Škoda's limit of 20 requests/hour for this car is used up — try "
                     "again in {min} min.", "rate_limited"),
}

# The bmw brand's sign-in errors: CarUpstreamError.kind → (form message, log reason)
_BMW_ERRORS = {
    "invalid_key": ("BMW doesn't accept that Client ID — copy it again from the MyBMW "
                    "portal (BMW CarData → Technical access) and check that CarData API "
                    "is ticked.", "invalid_key"),
    "vin_not_covered": ("BMW refused access to this car — check that CarData API is ticked "
                        "for your Client ID.", "vin_not_covered"),
    "rate_limited": ("BMW's daily limit for this Client ID is used up — try again "
                     "tomorrow.", "rate_limited"),
    "expired": ("The BMW code expired before it was approved — please start again.",
                "device_expired"),
    "denied": ("Access was declined on BMW's page — start again if that was a mistake.",
               "device_denied"),
}
_BMW_PENDING = ("BMW hasn't confirmed yet — approve the code on BMW's page, then press "
                "the button again.")


class CarAdapter:
    """Form login (brand + email + the owner's own API key + VIN) and local
    forward (mcp.py). One connector for every car brand; the account's brand
    lives in its blob. Brands: Škoda (MyŠkoda Public API) and BMW / Mini (BMW
    CarData, read-only).

    Škoda: start_login makes the sign-in's only upstream call — the car's budget
    is 20 requests/hour — and captures the key expiry + supported operations into
    the blob. BMW: start_login requests a device code (SecondFactorNeeded); the
    confirm page shows the code + BMW's link, and resume_second_factor fetches the
    token, proves the VIN and prepares the data container. Either way verify then
    checks the blob offline (it can't write to it)."""

    name = "car"
    display_name = "Car"
    authorize_template = "car_authorize.html"
    second_factor_template = "car_confirm.html"
    landing_template = "car.html"
    # Landing copy + link-preview card (app.adapter_routes probes these): the
    # generic "connect your Car account to Claude" reads wrong for a car.
    # The title doubles as the link-preview headline (iMessage shows the domain under
    # it, so no "| MissingMCP"); the search phrase "MCP server" lives in the description.
    landing_title = "Control your car from Claude, ChatGPT & Grok"
    landing_description = ("Hosted car MCP server: ask Claude, ChatGPT or Grok about "
                           "battery, range and locks, and pre-heat the cabin. Škoda, BMW "
                           "and Mini, your own API key, free beta.")
    og_image = "og-car.png"

    def __init__(self, config):
        self.forward = CarLocalForward(config)
        self._skoda = skoda.SkodaClient(config.car_skoda_api_base)
        self._bmw = bmw.BmwClient(config)

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("car_email", "")

    def start_login(self, form: Mapping[str, str]) -> LoginOk:
        brand = form.get("car_brand", "")
        email = form.get("car_email", "").strip()
        api_key = form.get("car_api_key", "").strip()
        if brand not in BRANDS:
            raise LoginError("Please pick your car's brand.", reason="invalid_form")
        if "@" not in email:
            raise LoginError("Please enter a valid email address.", reason="invalid_form")
        if brand == "bmw":
            return self._start_bmw(form, email)
        if not api_key:
            raise LoginError("Please paste the API key from the MyŠkoda app.",
                             reason="invalid_form")
        try:
            vin = skoda.normalize_vin(form.get("car_vin", ""))
        except CarUpstreamError:
            raise LoginError("A VIN has 17 letters and digits — copy it from the MyŠkoda app "
                             "(same screen as the key).", reason="invalid_form") from None
        try:
            reply = self._skoda.probe_sync(api_key, vin)
        except CarUpstreamError as e:
            message, reason = _LOGIN_ERRORS.get(
                e.kind, ("Škoda didn't answer — please try again in a moment.",
                         "upstream_error"))
            minutes = -(-e.retry_after_s // 60) if e.retry_after_s else 60
            raise LoginError(message.format(min=minutes), reason=reason) from None
        blob = {"v": 1, "brand": brand, "api_key": api_key, "vin": vin,
                "expires_at": reply.expires_at,
                "operations": skoda.operations((reply.data or {}).get("vehicle", {}))}
        return LoginOk(account_key=normalize_account_key(email), blob=json.dumps(blob))

    @staticmethod
    def credential_subject(blob: str) -> str:
        """The car a blob grants access to (brand + VIN): the identity the upstream
        actually proved, unlike the typed email that is our account_key. oauth._finish
        revokes the account's tokens and pending codes when a sign-in changes it."""
        d = json.loads(blob)
        return f"{d['brand']}:{d['vin']}"

    def _start_bmw(self, form: Mapping[str, str], email: str) -> SecondFactorNeeded:
        try:
            client_id = bmw.normalize_client_id(form.get("car_bmw_client_id", ""))
        except CarUpstreamError:
            raise LoginError("A Client ID looks like 1a2b3c4d-… (36 characters) — copy it "
                             "from the MyBMW portal: BMW CarData → Technical access.",
                             reason="invalid_form") from None
        try:
            vin = skoda.normalize_vin(form.get("car_bmw_vin", ""))
        except CarUpstreamError:
            raise LoginError("A VIN has 17 letters and digits — it's in the MyBMW app and "
                             "on the BMW CarData page.", reason="invalid_form") from None
        try:
            device = self._bmw.start_device(client_id)
        except CarUpstreamError as e:
            raise _bmw_login_error(e) from None
        return SecondFactorNeeded(state={"brand": "bmw", "email": email,
                                         "client_id": client_id, "vin": vin, **device})

    @staticmethod
    def second_factor_fields(state: object) -> dict[str, str]:
        """The confirm page's placeholders (oauth._second_factor_fields)."""
        d = state if isinstance(state, dict) else {}
        return {"USER_CODE": d.get("user_code", ""), "VERIFY_URL": d.get("verification_uri", "")}

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        """BMW only: one token request per "I've approved it" click. Pending
        re-prompts with the same state; then the VIN must be a PRIMARY mapping of
        this BMW account (the identity credential_subject relies on)."""
        if not isinstance(state, dict) or state.get("brand") != "bmw":
            raise LoginError("Car sign-in does not use a verification code.")
        if time.time() > state["expires_at"]:
            raise _bmw_login_error(CarUpstreamError("expired"))
        try:
            resp = self._bmw.poll_token(state["client_id"], state["device_code"],
                                        state["code_verifier"])
        except CarUpstreamError as e:
            if e.kind == "pending":
                raise SecondFactorError(_BMW_PENDING, state) from None
            if e.kind == "upstream_error":     # BMW hiccup (a 500 seen live): keep the code
                raise SecondFactorError("BMW didn't answer — press the button again in a "
                                        "moment.", state) from None
            raise _bmw_login_error(e) from None
        try:
            tokens = bmw.tokens_from(resp)
            if state["vin"] not in self._bmw.primary_vins(tokens["access_token"]):
                raise LoginError("This BMW account isn't the primary user of a car with that "
                                 "VIN — check the VIN in the MyBMW app.",
                                 reason="vin_not_covered")
            container_id = self._bmw.ensure_container(tokens["access_token"])
        except CarUpstreamError as e:
            raise _bmw_login_error(e) from None
        blob = {"v": 1, "brand": "bmw", "client_id": state["client_id"], "vin": state["vin"],
                **tokens, "container_id": container_id, "operations": []}
        return LoginOk(account_key=normalize_account_key(state["email"]),
                       blob=json.dumps(blob))

    def verify(self, blob: str) -> str:
        """Offline gate for verify-then-persist: start_login already proved the key
        against the upstream in this same request; a second call would spend
        another unit of the car's 20/h budget for nothing."""
        try:
            d = json.loads(blob)
            brand = d["brand"]
            if brand == "bmw":
                ok = bool(d["client_id"] and d["vin"] and d["access_token"]
                          and d["refresh_token"] and d["container_id"]
                          and isinstance(d["access_expires_at"], int)
                          and d["operations"] == [])
            else:
                expires = _parse_ts(d["expires_at"])
                ok = (brand in BRANDS and d["api_key"] and d["vin"]
                      and isinstance(d["operations"], list)
                      and expires is not None and expires > datetime.now(timezone.utc))
        except (ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            raise LoginError("The car sign-in could not be completed — please try again.")
        return BRANDS[brand]


def _bmw_login_error(e: CarUpstreamError) -> LoginError:
    message, reason = _BMW_ERRORS.get(
        e.kind, ("BMW didn't answer — please try again in a moment.", "upstream_error"))
    return LoginError(message, reason=reason)
