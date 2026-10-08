# Choose the throttle policy

Type: grilling
Status: resolved
Blocked by: 01, 02

## Question

Given the failing-login profile and how Garmin's edge limits sign-ins, which
attempts do we stop before they reach Garmin? Decide: which failure kinds to
act on; keys (login email / hashed user IP / OAuth client / per-egress
budget); limits and durations (escalation?); in-memory vs DB state; how it
interacts with the existing 5 min account cooldown and egress breaker; and
how a legitimate user always gets in within 5 minutes. Include
concurrency: whether to allow only one in-flight sign-in per login email
("Profile failing logins" found overlapping attempts are the main driver
the account cooldown can't stop).

## Answer

Decided with the operator 2026-10-08 (grilling). Terms: **in-flight sign-in**,
**post-connect hold**, **egress rest** (`CONTEXT.md`).

**What we let through to Garmin**
1. **One in-flight sign-in per login email.** A concurrent second attempt is
   turned away at once (not queued, not coalesced): "A sign-in for this Garmin
   account is already in progress. Wait a minute, then try again." The slot is
   held until the first attempt really finishes at Garmin — including a thread
   whose form already timed out.
2. **Post-connect hold, 2 min**, started only by a real connection (`ok`, or
   MFA completed) — never by reaching `needs_mfa` alone (a wrong password
   lands there and must be able to go back and retry): "This Garmin account
   was just connected. If you're adding another device, try again in two
   minutes."
3. **Account cooldown after `blocked`: 5 min**, unchanged.

**Egress protection**
4. An egress trips on either: ≥ 2 distinct accounts blocked on it with no
   sign-in getting through in between (existing), **or ≥ 10 blocked in the
   last 60 min** (new — stays under Cloudflare's stock ~20 failures/h; busiest
   egress so far ~6/h).
5. **Egress rest escalates**: 30 min → 1 h → 2 h on repeated trips, back to
   30 min after a sign-in gets through it. Numbers to be tuned once "Capture
   what a blocked sign-in returns" shows the real block length.

**Form**
6. Form wait (`login_timeout`) 30 s → **75 s** (a blocked attempt runs 30–90 s;
   the early timeout is what sent users resubmitting).
7. Submit button disables on click and reads "Signing in… this can take up
   to a minute".

**Other**
8. State in process memory only (as today); no DB.
9. Measurement: `egress-health` plus two new `scope` values on
   `login-breaker-reject`: `in_flight`, `post_success`. No renames.
