# 06 — Measure what remains after the fix

Type: task
Status: resolved
Blocked by: 02, 05

## Question

About a week after the read-back fix (and the backfill, if
[05](05-backfill-decision.md) says yes) is live, re-run the seven-day
measurement from garmin-token-lifecycle 01's triage:

- how many accounts still hit stale-token failures
  (`worker-forward-auth-stale` / `worker-start-failed`), and why (password
  change, MFA reset, upstream invalidation — anything that isn't our own
  discarded rotation);
- the recovery rate and time-to-recovery of those still affected;
- the total problem volume the hourly digest still sees.

The answer gates two decisions: the mail channel
([07](07-mail-channel-decision.md) — if expiries collapse to near-zero, mail
may be unnecessary) and the triage rollout tuning (follow-up of
[08](08-autonomous-triage-design.md)). Aggregates only — public repo.

## Comments

- 2026-07-31: a **scheduled cloud agent** will resolve this ticket — one-off
  routine "Reliability 06 — post-fix measurement" fires 2026-08-07T14:30Z
  (7 days after the fix + backfill went live), measures the window from
  2026-07-31T14:00Z via PostHog, and opens a PR with the resolution
  (branch `research/post-fix-verification`). Don't work this ticket by hand
  before then unless the routine failed.

## Answer (2026-08-19)

Measured over the fresh window **2026-08-12 → 2026-08-19** (the original
post-fix window was lost: the scheduled routine never delivered its PR and
PostHog Logs retention aged the rows out — full caveats in the asset).

**The Garmin-token fixes worked.** Stale-token failures: **2 accounts / 2
incidents in 7.8 days** vs the 58-accounts/7d baseline (**−97 %**), and both
users re-signed in **within minutes** (2.4 and 7.8 min; baseline median ~35 h
with 1 in 3 never returning) — the read-back (2,310 persists in the window),
the backfill and the PR #16 re-auth copy together closed the loop. Real
worker faults: 2 isolated rc=3 crashes.

**Digest volume:** 3,841 error rows, 81 % of them the stream-teardown class
that PR #18 demoted to warn at the window's very end. Forward-looking loud
hours: **~11.6/day post-#18**, dropping to **~2.4/day** once the one NEW
class is resolved: **`mcp-forward-error`** (463 rows, ConnectError 428 /
ReadError 35, garmin only, 214 accounts, growing 27→109/day, dominated by
handshake methods incl. the non-standard `server/discover`) — flagged for a
new ticket; possibly user-facing (502 on initialize).

**Verdict for [ticket 07](07-mail-channel-decision.md): mail channel NOT
justified** — no population left to help (2 expiries/week, minutes-fast
recovery). Full analysis:
[assets/post-fix-verification-7d.md](../assets/post-fix-verification-7d.md).
