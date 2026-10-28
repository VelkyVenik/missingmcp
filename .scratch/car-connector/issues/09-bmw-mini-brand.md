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
- **Live smoke on Vaclav's i3 (2026-10-28), new Client ID:** device code → approved on
  `customer.bmwgroup.com/oneid/link` → token (scope `cardata:api:read openid
  authenticate_user`, 3599 s, refresh token) → VIN is the single PRIMARY mapping → container
  created → telematic read OK (battery 91 %, range 151 km, odometer 104 226 km). First attempt:
  the token poll got a **500** right as the user approved, then `access_denied` — now a 5xx
  re-prompts instead of starting over. Values are strings; is-open keys are **"OPEN"/"CLOSED"**;
  location, climate and charging time came back **null** (car not reporting them). **No
  rate-limit headers** on the reply, so the gateway's own 24 h count is all there is.
- Former left-overs, now moot or answered: the old Grok-bot Client ID was deleted, so no
  sharing; the 429 shape is still unseen. does a second device sign-in on the same Client ID
  keep the Grok bot's tokens alive; real quota behaviour (429 shape); which container keys the
  i3 actually returns (doors, climate).

## BMW Group widening (2026-10-28)

- The CarData portal lists BMW ConnectedDrive, Mini Connected, Toyota Supra Connect,
  Rolls-Royce Connected and My BMW Motorrad: one API, one BMW Group login. The `bmw` brand now
  covers them all (picker, form, site), labelled **tested on BMW only**; owners of the others
  are asked to report back to the operator.
- Container v2 (`missingmcp-v2`, new name because keys are fixed at creation and reuse goes
  by name): adds fuel level / litres / range, door lock state and windows, all names from BMW's
  Customer Telematics Data Catalogue (bmw.de/de-de/mybmw/public/cardata-telematic-catalogue/).
  Accounts on the v1 container keep working; a reconnect moves them to v2.
