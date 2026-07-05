# Finish Garmin to Target Shape + MissingMCP Home — Design

**Date:** 2026-07-05
**Status:** Approved design (pre-implementation).
**Extends:** `2026-07-05-multi-adapter-gateway-design.md` — this commits to that
spec's **steps 2 and 3** for the Garmin-only path, plus an operator revoke/status
CLI and a restructured landing. **Rohlik (step 4) is explicitly deferred.**
Step 1 (adapter seam) is already merged (`plan 2026-07-05-adapter-seam.md`).

## Problem

After the adapter-seam refactor, the gateway is generic in *code* but still
Garmin-shaped in its *schema* and *URLs*: the DB has `garmin_accounts` /
`garmin_user_key`, the connector lives at the bare `/mcp`, and the landing page
is a single Garmin onboarding page. To be "the way it should be" — the target
shape a second adapter will slot into — three structural things must change, and
the operator needs a real way to see and revoke access. This work stops short of
adding Rohlik; it brings the *Garmin* deployment to the target architecture.

## Decisions (brainstorm 2026-07-05)

| Decision | Choice | Rationale |
|---|---|---|
| Schema | Migrate to generic `accounts` + `adapter` columns now | Spec step 2. Do it while there are no real users (staging has one throwaway account). Schema then never changes for a new adapter. |
| Routing | Move to `/garmin/mcp` + path-scoped `.well-known` now | Spec step 3. Target URL structure; cheapest to break the connector now (staging re-onboard is trivial). No `/mcp` alias. |
| Revoke/management | **Operator CLI** (`scripts/status.py` + `scripts/revoke.py`), adapter-aware | Small trusted circle, ~1 operator. A web admin adds an auth surface to secure for no real gain; the spec's stated posture is "admin is CLI". |
| Home page | **Functional rozcestník** at `/`, per-adapter subpage at `/garmin` | MissingMCP umbrella that lists connectors and links to each; sober utility, not a marketing landing. Room for more connectors later. |
| Rohlik | Deferred | User asked to hold it. No `RemoteForward`, no rohlik adapter, no forward-strategy branch in proxy. |
| Re-auth UX, minor-findings cleanup | Out of scope | Not selected. Trivial cleanups may ride along opportunistically but are not features here. |

## Goals / Non-goals

**Goals**
- DB is generic (`accounts(adapter, account_key, blob_enc)`); a new adapter needs
  no schema change. One guarded migration; ciphertext preserved (no re-encryption).
- Garmin connector reachable at `/garmin/mcp` with correct path-scoped OAuth
  discovery, verified on staging before prod.
- Operator can, from one CLI command, see every connected account + its devices +
  last-used + tool usage, and revoke a whole account or a single device.
- `/` is a MissingMCP rozcestník listing connectors; `/garmin` carries the Garmin
  connect instructions.
- Existing behavior otherwise identical; test suite green throughout.

**Non-goals**
- Rohlik / `RemoteForward` / any second adapter (spec step 4).
- Web admin UI / authenticated admin surface.
- User self-service disconnect.
- Re-auth UX redesign; package rename `garmin_gateway`→`missingmcp`; clearing the
  deferred Minor review findings as a deliverable.
- Marketing/branded landing (chose the sober rozcestník).

## Part 1 — Schema migration (step 2)

Target schema:

```
accounts                          -- one row per (service, account)
  adapter      TEXT NOT NULL      -- 'garmin'
  account_key  TEXT NOT NULL      -- normalized lowercased login email
  blob_enc     TEXT NOT NULL      -- AES-256-GCM(adapter-defined JSON); Garmin: tokens
  created_at, updated_at
  PRIMARY KEY (adapter, account_key)

access_tokens : + adapter TEXT NOT NULL     (token_hash stays PK)
oauth_codes   : + adapter TEXT NOT NULL
oauth_clients : + adapter TEXT NOT NULL      -- DCR is per connector
tool_usage    : + adapter TEXT NOT NULL      (PK becomes (adapter, account_key, tool))
```

- Crypto unchanged: key = SHA-256(`GATEWAY_SECRET`), `nonce:ciphertext` hex.
- **Migration**, guarded by `PRAGMA user_version` (0 → 1), idempotent:
  `garmin_accounts` → `accounts(adapter='garmin', account_key=garmin_user_key,
  blob_enc=garmin_tokens_enc)`; add `adapter` (default/backfill `'garmin'`) on
  `access_tokens`/`oauth_codes`/`oauth_clients`/`tool_usage`; rename the
  `garmin_user_key` column to `account_key` on the three tables that have it
  (`access_tokens`, `oauth_codes`, `tool_usage` — `oauth_clients` has none); drop
  `garmin_accounts`. Ciphertext moves verbatim.
- **Code:** `store.py` CRUD becomes `(adapter, account_key)`-keyed; oauth/proxy
  pass `adapter` (already threaded from step 1) into store calls. The
  `garmin_user_key` identifier disappears from the codebase (the `token-issued`
  log field `garmin_user_key=` becomes `account_key=` — the one log-schema change
  in this work).
- **Blast radius — keep operator scripts working:** the column rename breaks every
  `scripts/*.py` that queries `garmin_user_key` (`status.py`, `revoke.py`,
  `usage.py`, and any DB reads in `health.py`). Plan A updates those column
  references so the scripts stay functional against the new schema (mechanical,
  keep-working); Plan B (Part 3) then does the UX polish on top. Scripts are not
  left broken between the two plans.
- **Tests:** old-schema fixture DB → migrate → data intact, `user_version` bumped,
  idempotent on re-run. Plus the existing suite adapted to the new column names.
- **Staging:** the one spike-test account migrates as the real rehearsal for prod.

## Part 2 — Path-scoped routing (step 3)

Per adapter `<a>` (only `garmin` now), routes move under the prefix:

| Method | Path | Notes |
|---|---|---|
| GET | `/.well-known/oauth-authorization-server/<a>` | RFC 8414; issuer = `PUBLIC_URL/<a>` |
| GET | `/.well-known/oauth-protected-resource/<a>/mcp` | RFC 9728; resource = `PUBLIC_URL/<a>/mcp` |
| POST | `/<a>/oauth/register` | DCR |
| GET/POST | `/<a>/oauth/authorize` | adapter form (+ MFA) |
| POST | `/<a>/oauth/token` | code exchange |
| POST/GET/DELETE | `/<a>/mcp` | authenticate → forward |

- Well-known routes sit at the **domain root with the path suffix** (RFC 8414
  inserts `/.well-known/...` between host and issuer path); registered centrally,
  not under the prefix. Metadata URLs *inside* the documents all carry the prefix.
- `oauth.metadata(config)` becomes `metadata(config, adapter)` and emits
  prefixed `issuer`/`authorization_endpoint`/`token_endpoint`/`registration_endpoint`.
  A new `protected_resource_metadata(config, adapter)` emits the RFC 9728 document
  (`resource`, `authorization_servers=[PUBLIC_URL/<a>]`).
- **No `/mcp` alias.** The bare `/mcp` and root `.well-known` are removed. The
  staging connector breaks → re-onboard (trivial; done twice already).
- **RISK — path-scoped OAuth discovery is the one real unknown.** Whether Claude's
  MCP OAuth discovery resolves a path-based server (`…/garmin/mcp`) via the
  path-inserted well-known URLs is the crux. Mitigation, mirroring the login
  spike: after deploying Part 1+2 to **staging**, add the connector fresh and
  confirm discovery + DCR + authorize complete. This "discovery spike" is the
  go/no-go before the same change touches prod. If discovery fails, fall back is
  documented (e.g. keep a root well-known that points at the garmin issuer) — but
  not built unless the spike shows it's needed.

## Part 3 — Operator revoke/status CLI

Polish the existing `scripts/` (no new runtime surface, no web auth):

- `scripts/status.py` — one overview table, adapter-aware: per account
  (`adapter`, `account_key`, created, last_used) with its device count (rows in
  `access_tokens`), and a tool-usage summary. Reads the SQLite DB directly (same
  as today), decrypts nothing (lists metadata only).
- `scripts/revoke.py` — `--account <adapter>:<key>` (or `--account <key>`
  defaulting to garmin) revokes all that account's tokens; `--device <token-hash-prefix>`
  revokes one device. Both delete from `access_tokens`; the encrypted account row
  is left intact unless `--purge` is also given (then the `accounts` row too).
- Run via `railway ssh --service gateway "python3 scripts/status.py"` etc.
- Tests: against an in-memory DB seeded with accounts+tokens+usage across the
  (now adapter-aware) schema.

## Part 4 — MissingMCP rozcestník + Garmin subpage

- **`/`** → `home.html`: "MissingMCP", one-line what-it-is, a list of connector
  entries. Each entry: name + one-liner + link to its subpage. Only Garmin now;
  the list is data-driven off the adapter registry (`name`/`display_name`) so a
  future adapter appears automatically. Footer: source link.
- **`/garmin`** → today's `landing.html` content, scoped to Garmin: what it is,
  how to connect (Server URL `PUBLIC_URL/garmin/mcp`, the sign-in flow), the
  password-never-stored security note, operator name/email, buy-me-a-beer, source.
- **404 catch-all** → the rozcestník (was: the Garmin landing).
- Templates: `landing.html` → `home.html` (rozcestin) + a per-adapter
  `garmin.html` (or a generic `connector.html` filled from adapter fields — pick
  generic if it stays simple, else a Garmin-specific page; the plan decides).
  Style stays the existing lightweight inline CSS; sober, not marketing.

## Implementation order — two plans

- **Plan A (structural, breaking):** Part 1 (schema + migration) then Part 2
  (path routing + path-scoped discovery). Touches `store.py`, `oauth.py`,
  `app.py`, `proxy.py`, templates for endpoints, and most tests. **Ends with the
  staging discovery spike** (deploy to Railway, re-add connector, confirm OAuth
  completes) as the go/no-go before prod.
- **Plan B (UX, non-breaking):** Part 3 (revoke/status CLI) + Part 4 (rozcestník
  + Garmin subpage). Independent of A's URL/schema churn except that both read the
  new `adapter` column, so B lands after A.

Each plan is TDD, green suite per task, frequent commits — same rhythm as step 1.

## Testing strategy

- **Migration:** old-schema fixture → migrate → assert data intact + idempotent +
  `user_version`. Real staging DB migrates as the prod rehearsal.
- **Routing:** unit-assert the two well-known documents carry prefixed
  issuer/resource; the existing oauth/proxy tests move to `/garmin/...` URLs.
- **Discovery spike (manual, staging):** the release gate for Part 2 — add the
  connector in a Claude client against the staging URL, confirm discovery→DCR→
  authorize→token→one tool call. Analogous to the Garmin-login spike.
- **CLI:** in-memory DB seeded across the adapter-aware schema; assert status
  output and that revoke deletes the right rows and leaves the rest.
- **Home:** `/` lists the Garmin connector and links to `/garmin`; `/garmin`
  renders the connect instructions with the prefixed URL; 404 serves the rozcestník.

## Open questions

- **Discovery-spike fallback:** if path-scoped discovery fails on staging, the
  minimal fallback (root well-known aliasing the garmin issuer) is designed then,
  not now.
- **`connector.html` generic vs `garmin.html` specific:** decided in Plan B by
  whether a generic template stays genuinely simple.
