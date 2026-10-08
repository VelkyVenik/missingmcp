# Wayfinder map: Stop failing logins from burning our egresses

Label: wayfinder:map
Created: 2026-10-06

## Destination

A sign-in throttling policy on missingmcp's own side — deciding which
sign-in attempts we let through to Garmin at all — is **decided, deployed
(via PR) and measured** (hybrid map). Done when, over 7 days:

1. `egress-health` shows no egress blocked for more than 2 h in a row, and no
   full rejection (every egress cooling down at once); and
2. a legitimate user is never locked out: after fixing a typo they get in
   within 5 minutes.

## Notes

- **Hybrid map** (overrides wayfinder's plan-only default): execution of the
  decided policy is carried into this map (ticket "Build, deploy and measure").
- **Every code change via feature branch + PR**; ask the operator first; show
  outward-facing text first. See [[branch-pr-workflow]], [[approve-public-posts]].
- **Public repo**: no e-mails, IPs of users, tokens or proxy credentials in any
  ticket or asset — aggregates and masked examples only.
- Vocabulary: **sign-in attempt**, **failing login**, **egress** (`CONTEXT.md`).
- Hard constraints: never persist or log passwords, not even hashes (CLAUDE.md
  invariant); a legitimate user may be slowed, never locked out; throttling
  state may live in the DB (survives deploys), user IPs only hashed and only
  for the throttle's lifetime.
- Candidate failure kinds (the data decides which matter): **blocked**;
  **needs MFA never completed** (= wrong password: Garmin shows its code page
  for any password, sends a code only after a correct one); **repeat sign-in
  right after a success**. Abuse (someone testing stolen credentials through
  us) is in scope — same lever, different cause.
- Candidate throttle keys: login email, the submitting user's IP (hashed),
  the OAuth client; plus a per-egress attempt budget as a high backstop.
- Prior context: reliability ticket 12
  (`.scratch/reliability/issues/12-garmin-sso-egress-rate-limited.md`) — the
  egress pool, per-account cooldown (5 min), egress breaker (30 min,
  distinct-accounts-without-success rule), `egress-health`, `egress_ip`.
  Skills to consult: grilling + domain-modeling for decision tickets.

## Decisions so far

<!-- one line per resolved ticket: [title](issues/NN-slug.md): gist -->
- [Profile failing logins in production](issues/01-profile-failing-logins.md): 42 % of 624 attempts blocked; the main controllable driver is overlapping/retried sign-ins of the same email (cooldown can't stop concurrent ones), then repeats right after a success; wrong passwords and abuse are not material.
- [Choose the throttle policy](issues/03-choose-the-throttle-policy.md): one in-flight sign-in per email; 2 min post-connect hold (real connection only); 5 min account cooldown; egress trips on 2 accounts w/o success or 10 blocked/h, escalating rest 30m→1h→2h; form wait 75 s + disabled submit; memory only; new reject scopes in_flight / post_success.
- [How Garmin's edge limits sign-ins](issues/02-how-garmin-edge-limits-sign-ins.md): POST-only rate limit (Cloudflare-style, ~1 h/1 day steps, ~20 failures/h per IP in the stock template) plus a plausible Garmin CAS failure throttle; mobile 429 is global; retries and failures — incl. invisible wrong passwords — are what count.

## Not yet specified

- **Abuse response** — no abuse signature in the first 2 days (max 3 emails
  per user IP); revisit only if the measurement after deploy shows one source
  driving many emails.

## Out of scope

- Official Garmin API — reliability ticket 13 (strategic, separate effort).
- More / residential egress IPs and proxy operations — operator's infra track.
- Changing garminconnect's login strategies beyond what we already configure.
