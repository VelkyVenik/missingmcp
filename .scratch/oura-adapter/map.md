# Wayfinder map: Oura adapter

Label: wayfinder:map

## Destination

An implementation-ready design spec at `docs/superpowers/specs/<date>-oura-adapter-design.md`
(following the shape of `2026-07-06-whoop-adapter-design.md`): auth model, token lifecycle,
tool set, beta gating, and testing approach all decided — so implementation can run as a
normal superpowers plan session with nothing left to decide. Implementation itself is NOT
part of this map.

## Notes

- Architectural template: the **whoop adapter** (`adapters/whoop/` — local forward strategy,
  in-process JSON-RPC MCP server with a `TOOLS` table, env-gated registration, fake-upstream
  e2e tests). Deviate only where Oura's API forces it.
- CLAUDE.md invariants apply throughout: verify-then-persist, `_bounded` blocking sign-in,
  `base.normalize_account_key`, encrypted blobs, no plain-text stderr.
- Standing default (mirrors the whoop decision): if Oura caps unapproved apps, ship as a
  capped Beta anyway with an honest note on the landing page — don't wait for approval.
- Beta tester: a friend of the operator with an active Oura membership, willing to test
  (confirmed 2026-07-17). He is the manual smoke-test release gate at implementation time.
- Skills to consult per ticket: `/research` (ticket 01), `/grilling` (tickets 03, 04).

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [Oura API v2 facts](issues/01-oura-api-research.md) — OAuth2 only (PATs dead since
  Dec 2025); single-use rotating refresh tokens (WHOOP-style, no offline scope);
  10-user cap until app approval; 19 read-only endpoints + a no-account sandbox;
  `personal_info` email is nullable and scope-gated. Full asset:
  [assets/oura-api-v2-research.md](assets/oura-api-v2-research.md).
- [Auth model decision](issues/03-auth-model-decision.md) — upstream OAuth (whoop
  pattern + WhoopApi refresh discipline); account_key stays normalized email with the
  `email` scope hard-required; explicit scope list, `email`+`daily` required, the rest
  degradable per-tool via granted-scopes persisted in the blob.
- [Tool set decision](issues/04-tool-set-decision.md) — full 1:1 coverage (~18 tools,
  every non-deprecated list endpoint, no by-id variants), `get_<endpoint>` naming,
  per-tool scope declared; membership-403 → friendly in-result error, never
  SessionExpired/502, never empty data.

## Not yet specified

<!-- empty — everything in scope is either decided or a live ticket. Landing page
     content and the home card flip live inside the spec ticket's skeleton section. -->

## Out of scope

- Implementation of the adapter (separate superpowers plan session — the destination is the spec).
- Launch marketing: notifying the `subscribers` table, directory listings (MCP Registry,
  Smithery, mcp.so) for the Oura connector — post-launch work, not part of finding the way.
- Official Oura partnership/approval beyond what the beta needs.
