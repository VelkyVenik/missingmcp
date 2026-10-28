# Wayfinder map: general car MCP connector

Label: wayfinder:map

## Destination

A general multi-OEM **car MCP connector** for missingmcp.com. First OEM backend =
Škoda via the official **MyŠkoda Public API**; then other VW Group brands that share
the same stack; then other OEMs later. This map covers research/design kickoff and
the first brand slice — not Intent→auto product UX.

## Notes

- **BYO X-API-Key model.** End users bring a key from the MyŠkoda app
  ([go.skoda.eu/api-keys](https://go.skoda.eu/api-keys)). Prefer framing as a
  self-host / user-owned-key connector; a hosted multi-tenant gateway that holds
  many users' keys is grey/red legally (same class of risk as Garmin) — treat
  carefully.
- **Public API ToS:** personal end-user use only. Docs:
  https://public.api.connect.skoda-auto.cz/docs — may not develop/provide/operate
  commercial products/services on this API. Commercial climate/control is *not*
  covered by Charging Data / GIS B2B contracts.
- **Intent→auto** (morning preconditioning via the Auta bot) is a *consumer* of
  this connector, not this ticket's implementation.
- **Do not build on** the unofficial `myskoda` / `skodaconnect` app API — EOL
  October 2026.
- Architectural template: mirror existing adapters (`adapters/whoop/`,
  `adapters/garmin/`, and the Oura design work under `.scratch/oura-adapter/`)
  where they fit — env-gated registration, `account_key` discipline, encrypted
  blobs, no plain-text secrets in logs. CLAUDE.md invariants apply.

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- Open research/design kickoff: [01 — Škoda MyŠkoda Public API](issues/01-skoda-myskoda-public-api.md).

## Not yet specified

- Connector slug / path layout (`/car/...` vs brand-scoped mounts).
- Auth UX for BYO key (form login vs paste-key page) and `account_key` source.
- Tool surface for the first Škoda slice (climate, aux heat, ventilation, charging/SoC).
- Whether missingmcp.com hosts multi-tenant at all vs docs-first self-host only.
- VW Group brand expansion and later OEM backends.

## Out of scope

- Intent→auto product UX (Auta bot morning preconditioning flow).
- BMW i3 / other non–VW-Group OEMs as first slice (separate OEM later).
- GIS / Charging Data B2B commercial contract (`gis-sales@cariad.technology`) —
  does not cover climate control and is the wrong product for this connector.
