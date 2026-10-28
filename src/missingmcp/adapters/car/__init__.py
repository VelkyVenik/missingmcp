from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Mapping
from ..base import LoginError, LoginOk, normalize_account_key
from . import skoda
from .mcp import CarLocalForward, _parse_ts
from .skoda import CarUpstreamError

BRANDS = {"skoda": "Škoda"}

# CarUpstreamError.kind → (form message, log reason)
_LOGIN_ERRORS = {
    "invalid_key": ("That API key isn't valid or has expired — create a new one in the "
                    "MyŠkoda app.", "invalid_key"),
    "vin_not_covered": ("This key doesn't cover that VIN — check the VIN, or create the "
                        "key for this car.", "vin_not_covered"),
    "rate_limited": ("Škoda's limit of 20 requests/hour for this car is used up — try "
                     "again in {min} min.", "rate_limited"),
}


class CarAdapter:
    """Form login (brand + email + the owner's own API key + VIN) and local
    forward (mcp.py). One connector for every car brand; the account's brand
    lives in its blob. Today the only brand is Škoda (MyŠkoda Public API).

    start_login makes the sign-in's only upstream call — the car's budget is 20
    requests/hour — and captures the key expiry + supported operations into the
    blob; verify then checks that blob offline (it can't write to it)."""

    name = "car"
    display_name = "Car"
    authorize_template = "car_authorize.html"
    second_factor_template = ""
    landing_template = "car.html"
    # Landing copy + link-preview card (app.adapter_routes probes these): the
    # generic "connect your Car account to Claude" reads wrong for a car.
    # The title doubles as the link-preview headline (iMessage shows the domain under
    # it, so no "| MissingMCP"); the search phrase "MCP server" lives in the description.
    landing_title = "Control your car from Claude, ChatGPT & Grok"
    landing_description = ("Hosted car MCP server: ask Claude, ChatGPT or Grok about "
                           "battery, range and locks, and pre-heat the cabin. Škoda first, "
                           "your own API key, free beta.")
    og_image = "og-car.png"

    def __init__(self, config):
        self.forward = CarLocalForward(config)
        self._skoda = skoda.SkodaClient(config.car_skoda_api_base)

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

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        raise LoginError("Car sign-in does not use a verification code.")

    def verify(self, blob: str) -> str:
        """Offline gate for verify-then-persist: start_login already proved the key
        against the upstream in this same request; a second call would spend
        another unit of the car's 20/h budget for nothing."""
        try:
            d = json.loads(blob)
            brand, expires = d["brand"], _parse_ts(d["expires_at"])
            ok = (brand in BRANDS and d["api_key"] and d["vin"]
                  and isinstance(d["operations"], list)
                  and expires is not None and expires > datetime.now(timezone.utc))
        except (ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            raise LoginError("The car sign-in could not be completed — please try again.")
        return BRANDS[brand]
