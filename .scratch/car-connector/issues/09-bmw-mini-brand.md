# 09 — Brand: BMW + Mini (BMW CarData)

Type: task
Status: claimed
Blocked by: 08

## Goal

Add `bmw` (BMW + Mini) as a read-only brand of the `/car` connector, on the official BMW
CarData customer API. Facts: [assets/08-other-brands-owner-api.md §1](../assets/08-other-brands-owner-api.md).

## Open questions (answer first, from Vaclav's working Grok-bot setup)

- The exact sign-in flow (device-code? PKCE?) and how it fits the authorize form: likely an
  extra "confirm in the BMW portal" step, MFA-style.
- Token lifetimes and refresh rotation (persist-before-use, like WHOOP?).
- Real REST quota; poll REST vs. hold the MQTT stream per account (one stream consumer at a
  time — would we steal the owner's other integration, e.g. the Grok bot?).
- Which data points the owner must enable, and the landing-page setup steps.
- `credential_subject` = `bmw:<VIN>`; what identity does CarData expose?

## Done when

- `adapters/car/bmw.py` implements the brand-client surface (`probe_sync` / `read` +
  `normalize_status` / `normalize_location`), no actions.
- Fake CarData upstream in tests; privacy greps as for Škoda.
- `/car` brand card flips from "Soon" to "Beta"; manual smoke on Vaclav's car.

## Progress (2026-10-28)

- Contract: BMW's own OpenAPI files, found in the portal bundle and copied to
  `assets/bmw-cardata-customer-api-v1.json` + `assets/bmw-cardata-device-code-flow.json`.
  Device code + PKCE S256 confirmed; BMW returns only `verification_uri`
  (`https://customer.bmwgroup.com/oneid/link`), the user types the code there; token errors:
  403 `authorization_pending` / `access_denied`, 401 `expired_token` / `invalid_client`,
  400 `slow_down`. Containers carry `state` ACTIVE/DELETED. The quota is not in the spec.
- Spec: `docs/superpowers/specs/2026-10-28-car-bmw-brand-design.md`. Implemented on
  `feat/car-bmw` (bmw.py, confirm step, read-only tools, rotating refresh, fake upstream tests).
- Left for the live test on Vaclav's i3: does a second device sign-in on the same Client ID
  keep the Grok bot's tokens alive; real quota behaviour (429 shape); which container keys the
  i3 actually returns (doors, climate).
