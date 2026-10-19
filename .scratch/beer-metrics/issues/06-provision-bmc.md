# Provision the BMC API token + confirm supporter fields

Type: task
Status: open (PARKED — external blocker)
Blocked by: —

## Parked (2026-07-24)

**Blocked by a BMC-side bug, not needed for the interim build.** `developers.buymeacoffee.com/dashboard`
returns nginx **400 "Request Header Or Cookie Too Large"** even in a clean incognito session, so an
API token can't be provisioned. Ticket 02 pivoted to **manual entry** (`scripts/add_beer.py`) as
the interim, which needs no BMC credential. This task stays parked until either BMC fixes the
portal (then finish the checklist below) or the effort pivots to the **webhook** path (configured
in `studio.buymeacoffee.com`, not the broken developers subdomain — would need a re-scope). Not on
the critical path for the spec (05).

## Question

Manual, operator-only (can't be done AFK — requires logging into the `venik` Buy Me a Coffee
dashboard). Graduated from ticket 01's Confidence & gaps; **re-scoped by ticket 02's decision** —
the interim is now manual CLI ingestion (the earlier API-polling design was itself superseded, and
webhook was never adopted), so the webhook-provisioning parts are dropped. Before the spec
(05) names exact wire fields with confidence, confirm from inside BMC:

1. The `venik` account can generate a **personal access token** at
   `developers.buymeacoffee.com/dashboard`, and the token actually authenticates
   `GET /v1/supporters` (Bearer).
2. Confirm the real field names on the one-time-supporter object — especially the **email**
   (`support_email` / `payer_email`), the **coffee count** (`support_coffees`), the price
   (`support_coffee_price`), currency (`support_currency`), and the **created-on timestamp**
   (`support_created_on`). The research took these from the Power Platform mirror; confirm against
   a live response.
3. Whether a **"private" payment** strips the email from the creator's own **API** copy (vs. only
   hiding it on the public wall) — this sizes the unmatched/anonymous tail. Test with a private
   test donation if feasible.
4. (Optional probe) whether an undocumented `since` / date query param exists on
   `GET /v1/supporters` — if so, month-bucketing gets cheaper than paging.

Resolution records: where the token lives (an env var, secret handling per CLAUDE.md — the token
is a secret, never logged), the confirmed supporter field names, and the private-payment email
behavior — the facts tickets 02 (already resolved) and 05 build on. Source:
`.scratch/beer-metrics/assets/bmc-capabilities.md` §Confidence & gaps.

## Comments

- 2026-10-18: still parked; manual entry carries the load. Batch of 22
  supporters (2026-10-04 … 10-16, 45 beers) recorded via `add_beer.record_beer`
  in one run (idempotent on email); the `beers` table holds 60 donations /
  112 beers. Dedup check before a batch: `~/.config/missingmcp/prod-read/beers.sh`.
  BMC dashboard not re-tested.
