# 06 — Write the car connector spec

Type: task
Status: resolved
Blocked by: 03, 04, 05

## Question

Write `docs/superpowers/specs/<date>-car-connector-design.md` from the resolved tickets, in
the shape of the whoop/oura specs (incl. testing approach: fake upstream e2e, manual smoke
gate on Vaclav's car, ToS/risk landing copy). Nothing new is decided here; any open question
found while writing goes back to the map as a ticket.

## Answer

Written 2026-10-28:
[docs/superpowers/specs/2026-10-28-car-connector-design.md](../../../docs/superpowers/specs/2026-10-28-car-connector-design.md).

- It consolidates tickets 01–05 and 07 in the whoop-spec shape: research summary, 7 decisions,
  architecture (form login, blob, `LocalForward`, per-car `tools/list`, tools, rate-limit
  handling, expiry notice), files, `/car` landing, security, testing, out of scope.
- **One refinement, confirmed with Vaclav:** the single sign-in upstream call
  (`GET ?include=info,operations`) runs in **`start_login`**, not `verify`. The adapter contract's
  `verify(blob)` can't write `expires_at`/`operations` into the blob, so `verify` only checks the
  blob shape offline. Sign-in still costs 1 of the 20/h, and verify-then-persist still holds.
  This amends ticket 04's "verify = one GET".
- Implementation-level details added in the spec (no new decisions): log events `car-login-*`,
  `car-upstream-error`, `car-key-expired`; test-only `CAR_SKODA_API_BASE`; versioned blob `v: 1`.
