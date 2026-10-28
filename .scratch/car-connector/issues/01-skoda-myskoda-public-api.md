# 01 — Škoda MyŠkoda Public API (first car-connector backend)

Type: research+design
Status: open

## Goal

Design (and later implement) the **first brand slice** of a general car MCP connector
for missingmcp.com: **Škoda via the official MyŠkoda Public API** as the first
backend. Product direction stays multi-OEM (Škoda → VW Group brands on the same
stack → other OEMs later); this ticket is the research/design kickoff so Claude
Code can write a design/spec or start adapter work without MissingMCP.com chat
context.

Vaclav will finish with Claude Code. Success for *this* ticket: a clear open
ticket + map (see [`../map.md`](../map.md)) with verified API facts and
architectural hints — no runtime code in this PR.

## Product direction (Vaclav, 2026-10-28)

- General **car connector**, not Škoda-only forever.
- Start Škoda, then VW Group, then others.
- **BYO-key** connector framing (user supplies `X-API-Key` from the MyŠkoda app).
- Hosted multi-tenant gateway that stores many users' keys is grey/red (Garmin-class
  risk); prefer self-host / user-owned key path.
- Intent→auto (morning preconditioning via Auta bot) **consumes** this connector —
  out of scope for this ticket's implementation.
- Do **not** build on unofficial `myskoda` / `skodaconnect` app APIs (EOL Oct 2026).

## API facts (verified 2026-10-28 — do not re-litigate unless checking live)

| Item | Value |
| --- | --- |
| Host | `https://public.api.connect.skoda-auto.cz` |
| OpenAPI | `/v3/api-docs` |
| Docs | https://public.api.connect.skoda-auto.cz/docs |
| Auth | `X-API-Key` from MyŠkoda app ([go.skoda.eu/api-keys](https://go.skoda.eu/api-keys)) |
| Key metadata | Response/header `X-API-Key-Expires-At`; keys expire; max ~5 tokens per VIN |
| Rate limit | ~20 req/h per VIN; `RateLimit-*` headers; `429` + `Retry-After` |
| Climate | AirConditioning start/stop; auxiliary heating; ventilation |
| Charging | Charging / SoC |
| Not available | Remote lock / honk |
| ToS | Personal end-user use only. May not develop/provide/operate commercial products/services on this API. Commercial → Charging Data / GIS (`gis-sales@cariad.technology`) — **does not cover climate**. |

## Architectural hints (mirror the repo)

Look at existing adapters and design work:

- `adapters/whoop/` — local forward, in-process MCP, env-gated registration.
- `adapters/garmin/` — form-login / worker pattern (BYO credential feel is closer to
  a paste-key form than upstream OAuth).
- `.scratch/oura-adapter/` — wayfinder map + ticket shape for a new connector.

Hard constraints from CLAUDE.md / architecture:

- Env-gated registration (adapter absent when env unset — not the same as retired).
- `account_key` = normalized identity (strip + lowercase via `base.normalize_account_key`);
  decide the identity source for a BYO-key connect (email? VIN? user-supplied label?).
- Encrypted blobs at rest; Bearer/client secrets hashed; **no plain-text secrets in logs**.
- Verify-then-persist; blocking upstream I/O via `_bounded` if any sync network calls
  land in OAuth handlers.
- Path-scoped connector under `/<adapter>/…` with RFC 8414 / 9728 discovery as needed.
- Structured JSON logs only (no plain stderr).

## Design questions to answer (frontier for this ticket)

1. **Connector shape:** one `car` adapter with pluggable OEM backends, or brand-scoped
   adapters that share helpers? Path prefix?
2. **Auth UX:** paste `X-API-Key` (+ VIN?) form; how expiry (`X-API-Key-Expires-At`)
   surfaces to the user; re-auth when the key dies.
3. **account_key:** what is the durable join key for a BYO-key account?
4. **Tool set for v1:** climate (AC / aux heat / ventilation) + charging/SoC; naming;
   how to map ~20 req/h per VIN into gateway throttling / friendly errors.
5. **Hosting stance:** docs-first self-host only vs optional hosted beta with clear
   ToS/risk copy (Garmin-class grey/red).
6. **Spec destination:** write `docs/superpowers/specs/<date>-car-connector-design.md`
   (or Škoda-first slice name) before implementation, following whoop/oura design shape.

## Out of scope

- Intent→auto product UX.
- BMW i3 / non–VW-Group OEMs as the first slice.
- GIS / Charging Data B2B commercial contract.
- Any production adapter code in the scratch-only kickoff PR.

## Success criteria

Claude Code (or a later agent session) can open this ticket + the map and either:

1. produce a design/spec under `docs/superpowers/specs/`, or
2. start adapter implementation,

without needing MissingMCP.com chat history. API facts above are already verified;
only re-check against live `/v3/api-docs` / docs if something looks stale.
