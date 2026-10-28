# 01 — Škoda Public API + VW Group facts

Type: research
Status: resolved
Blocked by: —

## Question

What exactly does the MyŠkoda Public API offer and constrain, live, as of 2026-10 — and do
other VW Group brands offer an equivalent public API? Primary sources only (live
`/v3/api-docs`, https://public.api.connect.skoda-auto.cz/docs, official brand announcements).
Produce a markdown asset under `.scratch/car-connector/assets/` linked from this ticket.

1. **Endpoints**: full list from the live OpenAPI — read (status, SoC, charging, climate
   state, position?) and actions (AC start/stop + params, aux heating, ventilation, charging).
2. **Key scope**: is an `X-API-Key` bound to a user (all their VINs) or to one VIN? How does a
   client learn which VIN(s) a key covers?
3. **Identity**: does any endpoint return a stable user identity (email, user id, profile)?
   (Decides the `account_key` source.)
4. **Expiry**: key lifetime, `X-API-Key-Expires-At` semantics, renewal; error shape for an
   expired/revoked key (401 vs 403 body).
5. **Rate limits**: ~20 req/h — per VIN, per key, or both? Do actions and reads share the
   budget? `RateLimit-*` / `Retry-After` exact semantics.
6. **Action semantics**: synchronous or async (operation id + polling)? Error/pending states
   (car asleep, offline, unsupported on this model).
7. **ToS**: exact wording on personal use, third-party apps, storing keys on someone's behalf.
8. **VW Group**: do VW, Cupra/Seat, Audi have an equivalent official public API (same host
   family / same shape)? Status and links only.

## Seed facts (verified 2026-10-28 by the kickoff — re-check live)

| Item | Value |
| --- | --- |
| Host | `https://public.api.connect.skoda-auto.cz` |
| OpenAPI | `/v3/api-docs` |
| Auth | `X-API-Key` from MyŠkoda app ([go.skoda.eu/api-keys](https://go.skoda.eu/api-keys)) |
| Key metadata | `X-API-Key-Expires-At`; keys expire; max ~5 tokens per VIN |
| Rate limit | ~20 req/h per VIN; `RateLimit-*` headers; `429` + `Retry-After` |
| Climate | AirConditioning start/stop; auxiliary heating; ventilation |
| Charging | Charging / SoC |
| Not available | Remote lock / honk |
| ToS | Personal end-user use only; no commercial products/services. Commercial → Charging Data / GIS (`gis-sales@cariad.technology`), which does not cover climate. |

## Answer

Full findings, with source links and an endpoint table:
[assets/01-skoda-public-api-facts.md](../assets/01-skoda-public-api-facts.md). Raw contracts:
[skoda-public-api-1.1.0.yaml](../assets/skoda-public-api-1.1.0.yaml) (the curated contract,
which is authoritative) and [skoda-openapi-v3.json](../assets/skoda-openapi-v3.json) (the
generated one).

- **Surface:** 12 operations. There is 1 read, `GET /api/v1/vehicles/{vin}?include=…`, covering
  status, fuel, odometer, parking position, AC, aux heating, ventilation, charging/SoC, charging
  profiles and supported `operations[]`. There are 11 actions: charging start/stop/limit/mode,
  charging-profile update, AC start/stop, aux heating start/stop, ventilation start/stop. There
  is no lock/honk and no list-vehicles endpoint.
- **Key scope:** a key is bound to the set of VINs the user picked when creating it. The API
  cannot list them, so the user must supply the VIN(s). A VIN outside the set gets
  `403 api-key-not-authorized`.
- **Identity:** no endpoint returns an email or user id. `account_key` has to come from the VIN
  or from user input (ticket 04).
- **Expiry:** `X-API-Key-Expires-At` (RFC 3339) is on every success. An expired key gets
  `401 problems/api-key-expired`; an invalid key gets `401 about:blank` (observed live).
  Renewal happens only in the app. The lifetime is undocumented (ticket 02).
- **Rate limit:** 20 requests per hour, "per VIN" according to the landing page. The changelog
  and the 429 text say "per key", so the docs contradict each other (ticket 02). Every
  authenticated request counts, 5xx included; 401/403 don't count. Headers are
  `RateLimit-Limit/Remaining/Reset` (reset in seconds) plus `Retry-After` on 429. The car has
  its own separate throttle, `429 vehicle-not-accepting-requests`.
- **Action semantics:** an action returns `202` with no body, no operation id and no status
  endpoint. You confirm the effect by re-reading the state, which costs quota. A `504` means
  "vehicle did not respond in time". `422` is `operation-not-supported` or `operation-disabled`.
- **Surprise:** `startAuxiliaryHeating` requires the user's **S-PIN** (`spin`) in the body
  (ticket 05).
- **ToS:** "personal end-user use only … may not be used to develop, provide, or operate
  commercial products or services". Nothing is said about hosted relays or storing keys on
  someone's behalf. In-app terms are still to be checked in ticket 02.
- **VW Group:** as of 2026-10-28, no equivalent owner-facing public API exists for VW, Audi,
  Cupra or Seat. The group closed the old brand-app API to third parties in May 2026, and its
  only official offering is GIS B2B (Charging Data, subscription, charging only). Škoda is
  alone for now.
