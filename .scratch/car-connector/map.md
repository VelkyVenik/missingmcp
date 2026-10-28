# Wayfinder map: car connector (Škoda first)

Label: wayfinder:map

## Destination

An implementation-ready design spec at `docs/superpowers/specs/<date>-car-connector-design.md`
(shape of `2026-07-06-whoop-adapter-design.md`) for the **Škoda slice** — upstream = the
official **MyŠkoda Public API**: connector shape/slug, BYO-key auth UX + `account_key`,
key expiry/re-auth, v1 tool set, rate-limit handling, hosting copy, testing approach — all
decided, with seams left for further OEMs. Implementation is NOT part of this map; it runs
afterwards as a normal plan session.

## Notes

- **More OEMs will follow** (other VW Group brands, then others). The spec designs Škoda
  only, but must not paint a multi-upstream connector into a corner.
- **Hosting (decided 2026-10-28):** hosted beta on missingmcp.com, free, each user pastes
  their **own** `X-API-Key` (encrypted blob). Prominent ToS/risk copy on the connector page
  (Public API = personal end-user use only; no commercial service). Same stance as Garmin:
  no monetization, no promotion.
- **Visibility (decided 2026-10-28, revised same day):** own landing page `/car` (brand
  list) is what gets promoted. Homepage priority Garmin > car > WHOOP: car gets a hero
  teaser line + the first card in "Also on MissingMCP", Garmin keeps the hero. Not in
  campaigns or MCP directories — for now (see Out of scope). Ships
  enabled, labelled beta (no env kill switch).
- **v1 includes actions** (decided 2026-10-28): climate / aux heat / ventilation start-stop,
  not just read state. Details (confirmation, temperature params, idempotency) → tool-set ticket.
- **Live testing:** Vaclav owns a Škoda with MyŠkoda and generates a key — he is the manual
  smoke-test release gate at implementation time.
- **Do not build on** the unofficial `myskoda` / `skodaconnect` app API — EOL October 2026.
- **Vocabulary** (`CONTEXT.md`): the connector is `car`; a **brand** (not make/OEM) is chosen
  per account and decides its **upstream** (the Škoda Public API is the `skoda` brand's upstream).
- Architectural template: the **whoop adapter** (local forward strategy, in-process MCP
  `TOOLS` table, env-gated registration, fake-upstream e2e tests) and `.scratch/oura-adapter/`
  for the map/spec shape. CLAUDE.md invariants apply: verify-then-persist, `_bounded`
  blocking sign-in, `base.normalize_account_key`, encrypted blobs, no secrets in logs,
  structured JSON only.
- Skills per ticket: `/research` (01), `/grilling` + `/domain-modeling` (03, 04, 05).

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [Škoda Public API + VW Group facts](issues/01-skoda-public-api-research.md) — real contract
  is `/docs/myskoda-public-api.yaml` (1.1.0): 1 read `GET /vehicles/{vin}?include=…` + 11
  actions (202, no op id, confirm only by re-read); key covers user-chosen VINs, no VIN listing,
  **no user identity**; 401 `api-key-expired`; 20 req/h (per VIN vs per key contradictory);
  aux heating needs the **S-PIN**; **no equivalent VW/Audi/Cupra/Seat public API** today.
- [Live key against own Škoda](issues/02-live-key-smoke.md) — key only via mobile app, fixed
  ~6-month life, max 5/account, VIN copyable on the same screen; 20/h **per VIN** (shared by all
  keys), fixed window, 403/404 free; no list endpoint; action 202 instant but a sleeping car
  needs ~2 min (awake ~4 s), freshness only via `carCapturedTimestamp`; `operations[]` per car;
  full read leaks GPS/address/plate.
- [Connector shape and slug](issues/03-connector-shape-slug.md) — one generic `/car` connector
  (adapter `car`; one team connector in Claude), brand picked per account on the authorize form,
  v1 one account = one brand; brand-neutral tools + "what can this vehicle do" tool; `/car`
  landing lists brands; ships as beta without a kill switch; escape hatch `car-<brand>`.
- [VW Group owner API availability](issues/07-vw-owner-api.md) — **no** official owner API for
  VW, Audi, Cupra/Seat or Porsche, none announced with a date; VW closed the brand-app API
  (CW21 2026) and is tightening it; GIS is B2B charging only. Škoda's API sits on Škoda's own
  gateway. A future key- or OAuth-based brand would still fit the `/car` picker. Watch list in
  the asset.
- [BYO-key auth, account_key, expiry](issues/04-auth-account-key.md) — form: brand + typed
  (unverified) email = `account_key` + key + one VIN; verify = one `GET ?include=info` via
  `_bounded` (401/403/429 re-render); expiry → re-auth 401, plus an in-tool notice 14 days
  ahead; S-PIN never stored, so no aux heating in v1.
- [v1 tool set and rate-limit handling](issues/05-tool-set-rate-limits.md) — 10 brand-neutral
  tools (capabilities, status, location, climate/ventilation/charging start-stop, charge limit),
  `tools/list` filtered by the stored `operations[]`; location only in its own tool; actions
  return at once with no polling; `budget` in every result, 60 s status cache, friendly
  429/422/504 tool errors.
- [Write the car connector spec](issues/06-write-spec.md) — **destination reached**:
  `docs/superpowers/specs/2026-10-28-car-connector-design.md`; the sign-in GET runs in
  `start_login`, and `verify` checks the blob offline (amends 04).

## Not yet specified

- Second brand: no VW Group brand has an official owner API today (see the VW research
  ticket). Revisit when one ships: how its sign-in slots into the brand picker, and whether one
  account may later hold several brands.
- Later tools: charge mode, charging profiles and timers; auxiliary heating (needs an S-PIN
  stance).
- Later account hardening: email verification (confirm link), email reminder before key
  expiry, several VINs per account.
- Beta gating / user caps, if the Public API's per-key or per-VIN limits make a hosted beta
  painful at scale.

## Out of scope

- Implementation (adapter code, landing page) — follows the spec as a plan session.
- Homepage restructure into sport / car sections — planned later as its own effort.
- Intent→auto product UX (Auta bot morning preconditioning) — a *consumer* of this connector.
- Non–VW-Group OEMs (e.g. BMW) as the first slice.
- GIS / Charging Data B2B commercial contract — doesn't cover climate; wrong product.
