# 01 — Raise the WHOOP member cap (100 → 1,000)

Type: task
Status: ready-for-human — request submitted 2026-10-30, waiting for WHOOP
Blocked by: —

## Context

The WHOOP app hit its 100-member cap on 2026-10-26 (last completed `upstream-oauth-callback`
21:20 UTC). Since then every new sign-in stops on WHOOP's consent page: ~140
`upstream-oauth-start` events over four days (69 on 10-29), with no callback. Existing members are
unaffected (`whoop-refresh-ok` keeps running). On 2026-10-30, 101 whoop accounts were in the DB and 94 held a live token.

Usage at 100 members (PostHog `mcp_tool_calls`, data tools only): 450–700 calls/day over the
last two weeks, plus ~80 token refreshes/day. One tool call = one WHOOP API request, so
1,000 members means ~8,000/day against the current 10,000/day and 100/min limits.

## Steps

1. ~~Submit WHOOP's limit-increase form (developer dashboard): Growth tier (1,000 members),
   300 calls/min, 30,000 calls/day. The drafted answers are in the 2026-10-30 session; the public URL
   is https://missingmcp.com/whoop. Attach screenshots of a Claude conversation using WHOOP data.~~ Done 2026-10-30.
2. Wait for WHOOP's answer.
3. Once raised: revert the "Full for now" copy on `/whoop` (`templates/whoop.html`) and the
   home card (`templates/home.html`), update the cap in both and in `tests/test_app.py`
   (`test_whoop_states_its_user_cap`, `test_home_lists_upcoming_connectors`), and update the
   member-limit note in `README.md`.
4. Check that `upstream-oauth-callback` events for whoop resume.

## Later

- Dormant accounts (never refreshed since sign-up) may still hold member slots. Find out
  whether revoking them (`DELETE /v2/user/access`) frees a slot before relying on it.
- Growth → next tier needs 50% of the cap connected; watch it.

## Done when

- WHOOP confirms the higher cap and new members connect again.
- The site copy states the new cap; tests pass.
