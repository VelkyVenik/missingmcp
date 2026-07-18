# 05 — Write the Oura adapter design spec

Type: task
Status: open
Blocked by: 02, 03, 04

## Question

Assemble the destination artifact: `docs/superpowers/specs/<date>-oura-adapter-design.md`,
following the structure of `2026-07-06-whoop-adapter-design.md` (rationale, data flow,
module layout under `adapters/oura/`, env vars, beta gating, landing page skeleton,
testing approach — fake Oura upstream e2e per `tests/conftest.py::fake_whoop` pattern,
manual smoke test by the beta tester as release gate). The spec must also address three
facts from ticket 01's research: the **per-application aggregate rate-limit bucket**
(all gateway users share it — does the gateway need its own throttle?), **membership
lapse → 403 mid-life** (per the Tool set decision: a friendly in-result error — "renew
your Oura membership" — NOT SessionExpired/502, never empty data), and the official
**sandbox** (`/v2/sandbox/usercollection/*`, no account needed) as a possible extra
testing layer beside the fake upstream. Review with the operator; the map is done when
this spec is merged and nothing is left to decide before implementation.
