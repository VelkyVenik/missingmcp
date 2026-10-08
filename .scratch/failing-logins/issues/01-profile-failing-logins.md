# Profile failing logins in production

Type: task
Status: resolved
Blocked by: —

## Question

What do failing logins actually look like in production since the proxy
rollout (2026-10-04 18:55 UTC)? From Railway logs (`garmin-login-attempt`,
`login-breaker-*`, `mfa-resume-*`, `token-issued`, `authorize-post`,
`egress-health`), aggregate — never publish e-mails/IPs:

- per login email: attempt sequences, outcome mix, gaps between retries,
  how many end in a connected account vs never;
- **needs MFA never completed** share (wrong-password proxy) and how often
  those users retry;
- repeat sign-ins right after a success: how common, how fast (double-submit?);
- whether few sources (OAuth client, and the submitting user's IP if any log
  carries it) drive many distinct login emails — the abuse signature;
- correlation: in the hours an egress got burned, which failure kinds dominated
  the attempts on it just before.

Answer: the failure kinds ranked by how much egress-burning traffic they cause,
with the numbers, plus which throttle keys are actually observable in logs.

## Answer

Data: Railway app logs of all 17 gateway deployments plus Railway HTTP logs
of `POST /garmin/oauth/authorize`, 2026-10-04 20:32 → 2026-10-06 16:46 UTC.
Aggregates only; the analysis script stays local (it handles e-mails/IPs).

**Volume.** 624 sign-in attempts from 375 login emails: 264 ok, 97 needs MFA,
263 blocked (42 %). 323 emails (86 %) ended up connected; 49 (13 %) only ever
got blocked. Median 1 attempt per email; 26 emails with ≥ 4 attempts made
140 attempts (22 %), and 17 of those 26 did connect in the end.

**Failure kinds, ranked by how much Garmin-bound traffic they add:**

1. **Retrying after a block — the big one.** 184 attempts followed the same
   email's blocked attempt (99 within 2 min). Even after the 5 min account
   cooldown shipped (#39), 47 of 109 such retries still reached Garmin within
   5 min, mostly 40–120 s after the block: **overlapping attempts**. Attempts
   are logged on completion and a blocked one runs 30–90 s (portal waits);
   a second submit — the user resubmitting after our 30 s form timeout while
   the abandoned thread keeps going, a double click, a second tab — starts
   before the first one has set the cooldown. The cooldown only stops
   *sequential* retries; nothing serialises **concurrent** sign-ins of one
   email.
2. **Repeat sign-in right after a success.** 65 attempts followed the same
   email getting through; 31 of them within 60 s (double-submit shape); 43
   within 1 h after an `ok` (7 % of all attempts). 32 of the 263 blocks (12 %)
   hit an email that had just got through — pure waste.
3. **Wrong password (needs MFA never completed) — small.** 97 needs-MFA
   attempts over 82 emails, and 79 of those emails did connect: needs MFA is
   overwhelmingly real MFA users. Only 3 emails reached MFA and never
   connected. Wrong passwords are not a material driver.
4. **Abuse (one source, many emails) — not observed.** Distinct emails per
   user IP: 1 → 78 IPs, 2 → 11, 3 → 1 (max 3). Caveat: only 232 of 839 login
   POSTs matched an HTTP log line by timestamp. OAuth clients per email are
   often > 1, but that is Claude registering a fresh client per connection
   attempt (glossary: OAuth client), not a signal.

**Before a burn.** In the 5 egress-health windows with 0 through / ≥ 3 blocked,
the preceding 2 h held 7–25 attempts from 2–16 emails with 3–9 repeat
attempts — no single dominant source; e.g. one window was 7 attempts, all
blocked, from 2 emails retrying into the block.

**Observable throttle keys.** In-app: login email (`login-start`,
`garmin-login-attempt`), OAuth client (`authorize-post`). The submitting
user's IP is available to the app at request time (`request.client.host`,
already used by the per-IP rate limiter) but is not in any sign-in event;
offline it is only in Railway HTTP logs, matched by time (weak).

**Implication for "Choose the throttle policy":** the lever with the most
traffic behind it is **one sign-in per email at a time** (coalesce/reject a
concurrent attempt while one is in flight, including abandoned timed-out
threads), then **no new sign-in shortly after the same email got through**,
then the per-egress budget. Account cooldown alone does not cover (1).
