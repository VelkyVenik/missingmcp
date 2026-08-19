# 07 — Proactive user notification: do we need a mail channel at all?

Type: grilling
Status: open
Blocked by: 06

## Question

garmin-token-lifecycle 01 established that the re-auth 401 works as a
protocol but not as a notification (median recovery ~35 h, one in three never
returns) and that proactive e-mail is the only option addressing the
mechanism — the user isn't at the keyboard. But the read-back fix should
remove most expiries at the source.

With post-fix numbers ([06](06-post-fix-verification.md)) in hand, decide
whether the *remaining* expiry volume (password changes, MFA resets,
upstream invalidation) justifies building an outbound mail path — the
gateway has none today (`/subscribe` stores an address, sends nothing). If
yes: provider, where the credentials live, policy (at most one mail per
expiry, unsubscribe, or it becomes spam), and the copy (operator's voice —
show before anything outward-facing ships). If the volume is negligible,
close as not-needed.

## Comments

- 2026-08-19: ticket 06's measurement verdict: **not justified** — 2
  expiries/week, both recovered in minutes; there is no population left for
  proactive mail to help. Presented for closing; the operator's answer
  pivoted to the OPERATOR-facing channel instead ("the daily summaries only
  go to Slack — maybe better by e-mail"), now ticket
  [13](13-operator-channel.md). Left open deliberately: if 13 lands on
  e-mail, an outbound mail path exists anyway and this ticket's main cost
  objection disappears — re-decide then, against the (tiny) measured volume.
