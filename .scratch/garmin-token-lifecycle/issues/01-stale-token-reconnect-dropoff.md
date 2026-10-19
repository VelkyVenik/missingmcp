# 01 — Half the users hitting stale Garmin tokens never reconnect

Type: task
Status: resolved (2026-07-30 — superseded by the reliability map: tickets 02, 03, 06, 07)

Triaged 2026-07-27 — see "## Triage" at the bottom for the seven-day numbers,
which correct the first measurement and narrow the options down to one.

## Context

Found on 2026-07-26 while diagnosing why the PostHog alert
`Worker start failures ≥3/hour` fires several times a day. The alert itself was a
signal problem, fixed separately (`fix(worker): split stale credentials from real
worker start failures`, branch `fix/worker-start-signal`). This ticket is the
*user-facing* problem that diagnosis surfaced.

When an account's stored Garmin tokens go stale, `garmin_mcp` refuses to start and
the gateway answers the RFC 9728 re-auth 401. In theory the MCP client re-runs
authorization and the user signs in again — the designed self-heal path. In
practice only about half of them come through it.

## Measurement (24 h window ending 2026-07-26 ~09:00 UTC)

Log-derived (`worker-start-failed` rows, PostHog Logs) cross-referenced against
`accounts.updated_at` and `tool_usage.last_used` in the production DB:

- 23 failure rows across **17 distinct accounts** (of 149 accounts / 144 with a
  live token at the time)
- **7 accounts re-authed and resumed within minutes** — typical shape: failure
  08:05 → `accounts.updated_at` 08:12 → successful tool call 08:14
- **10 accounts never came back** — no re-login, no further tool call. Five of
  those failed the previous afternoon/evening, so they had 15+ hours of
  opportunity; the rest failed overnight and had less
- Every one of the 23 was a clean `rc=0` worker self-exit on stale tokens; none
  was an infrastructure fault

So a stale token costs roughly one in two affected users, and at ~17 accounts/day
that is a steady leak, not an edge case.

## Why it might be happening (unverified)

The 401 only helps a user who is *actively* driving the connector at that moment
and whose client surfaces the re-auth prompt clearly. Candidates worth checking
before designing a fix:

- Does the MCP client actually prompt on the 401, or does it silently show a
  broken tool call? (Differs per client — mobile / desktop / web.)
- Does the user understand that "reconnect" means signing in again on
  missingmcp.com, given the message is `Your Garmin session expired. Please
  reconnect the Garmin MCP server.`?
- Is the expiry itself avoidable? See ticket 02 — refreshed tokens are currently
  discarded, which may be shortening credential life.

## Options (not decided)

1. **Proactive notification** — detect stale credentials and e-mail the account a
   reconnect link. Needs an outbound mail path, which the gateway does not have
   today (`/subscribe` is storage-only, no mail is sent).
2. **Reduce expiries at the source** — ticket 02, if it turns out refresh
   persistence extends credential life.
3. **Better in-client copy** — the re-auth message is the one string the user
   reliably sees; make it carry the full instruction and the URL.
4. **Measure first** — add a funnel on the new `worker-forward-auth-stale` event
   → subsequent successful sign-in, so recovery rate is tracked continuously
   rather than hand-queried once.

Option 4 is cheap and makes the rest decidable; likely the right first step.

## Notes

- Do not put account e-mails in this repo — it is public. The numbers above are
  aggregates; the per-account detail is reproducible from the DB and PostHog Logs.

## Triage (2026-07-27)

Re-measured over seven days from the log stream instead of one day by hand, which
is what "option 4 — measure first" asked for. That option is now **done**, and the
result kills two of the other three.

Query: `logs` grouped by account, counting `worker-start-failed` /
`worker-forward-auth-stale` against a later `token-issued` or `mcp-response`.

| | 7 days |
|---|---|
| accounts hit | **58** (of 173 accounts — a third of the base) |
| failures | 99 |
| recovered (signed in or called again after the failure) | 30 |
| gone, but failed <12h ago (may not have tried yet) | 8 |
| **gone with 12h+ of opportunity** | **20** |
| of those, gone 12–72h / over 3 days | 12 / 8 |

The number that changes the decision:

| time from failure to recovery | accounts |
|---|---|
| within 15 min | **3** |
| 15 min – 4 h | 4 |
| over 4 h | 23 |
| **median** | **~35 hours** (2090 min) |

**Correction to the first measurement above:** it says seven accounts "re-authed
and resumed within minutes". That was a biased read of a single day — across seven
days only **3 of 58** come back within 15 minutes. The median recoverer takes a day
and a half.

And 11 of the lost accounts kept failing more than once, i.e. the client retried
and served the 401 challenge again, and the user still did not come back.

### What that means

The re-auth 401 works as a protocol but **not as a notification**. It only reaches
someone who is watching Claude at that moment, which is ~5% of cases. Everyone else
learns their connector is dead the next time they happen to ask a question — a day
and a half later, on average, and one in three never does.

So of the four options:

1. **Proactive notification — the only one that addresses the mechanism.** The user
   is not at the keyboard; something has to reach them where they are. Needs an
   outbound mail path, which the gateway does not have (`/subscribe` stores an
   address and sends nothing). This is the real work: pick a provider, hold the
   credentials, write the copy, and decide the policy — at most one mail per
   expiry, with an unsubscribe, or it becomes spam.
2. **Ticket 02 (persist refreshed tokens) — prevention, still unverified.** Doesn't
   help anyone already expired, but every expiry it prevents is an account that
   never enters this funnel. Cheap to test; do it first if it holds.
3. **Better in-client copy — marginal.** Improves the 5% who are present. Worth a
   sentence, not a project.
4. **Measure first — done.** This section is the answer.

### Why `ready-for-human` and not `ready-for-agent`

The implementation is not the blocker; the decision is. Sending mail to users is a
new outward-facing channel with privacy and deliverability consequences, it needs
an account and a secret somewhere, and the copy is the operator's voice. None of
that is an agent's call. Once the channel and the policy are chosen, the code
around it is small and can be specified as its own ticket.

Recommended order: verify ticket 02 first (cheap, might shrink the problem), then
decide on the mail channel.

## Comments

- 2026-07-30: carried forward on the **reliability map**
  (`.scratch/reliability/map.md`). The mail-channel decision is its ticket 07
  (blocked by post-fix verification, ticket 06); the copy improvement
  (option 3) is its ticket 03. Nothing further happens in this file.
