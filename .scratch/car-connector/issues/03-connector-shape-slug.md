# 03 — Connector shape and slug

Type: grilling
Status: resolved
Blocked by: 01

## Question

Is the Škoda slice mounted as a brand connector (`/skoda/mcp`) or a generic car connector
(`/car/mcp`) whose upstream is chosen per account — given what 01 found about other VW
Group brands, and given that more OEMs will follow? Also: adapter name, landing page path,
and how a later brand slots in without migrating existing accounts.

## Answer

Decided with Vaclav, 2026-10-28.

- **One generic connector `/car/mcp`**, adapter name `car`; OAuth under `/car/oauth/*`;
  landing page `/car`. Why: Claude team connectors are registered per team, and Vaclav wants
  exactly **one** car connector there, not one per brand. (`Adapter.name` is also the DB
  `adapter` column, the log field and the URL prefix, so this can't change cheaply later.)
- **Brand chosen per account on the authorize form**: brand picker first, then that brand's
  fields (Škoda: key + VIN; another brand could be its own upstream OAuth). The account's brand
  lives in its encrypted blob.
- **v1: one account = one brand** (several VINs of the same brand under one key are allowed).
  Multiple brands per account is a later blob extension.
- **Escape hatch:** a brand that doesn't fit `/car` gets its own adapter `car-<brand>`
  (e.g. `car-citroen`).
- **Brand-neutral tools** (`get_vehicle_status`, `start_climate`, …) plus a tool that tells the
  model **which tools this account's vehicle supports** (backed by Škoda's `operations[]`).
  The tool list itself is decided in 05.
- **Landing `/car`** lists brands, each with status, info, setup steps and links.
- **No env kill switch**: the connector ships enabled, labelled **beta**; the label comes off
  once it runs well.
- Glossary: **Brand** added to `CONTEXT.md`, and **Upstream** amended (the `car` adapter has
  one upstream per brand).
- Brand ids: `skoda`, then e.g. `volkswagen`. Vaclav plans VW soon, but ticket 01 found no
  VW public owner API; checked in [07 — VW Group owner API availability](07-vw-owner-api.md).
