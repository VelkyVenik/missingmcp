# 09 — Brand: BMW + Mini (BMW CarData)

Type: task
Status: needs-triage
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
