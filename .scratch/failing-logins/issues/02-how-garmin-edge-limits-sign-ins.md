# How Garmin's edge limits sign-ins

Type: research
Status: resolved
Blocked by: —

## Question

What is publicly known about how sign-ins to Garmin SSO (sso.garmin.com,
behind Cloudflare) get rate-limited or blocked, and how fast blocks decay?
Specifically: per-IP vs per-account limits; Cloudflare bot management /
leaked-credential / credential-stuffing detections that key on "many accounts
from one IP" or on failed logins; typical rate-limit windows and decay; what
the garminconnect / garth communities report (429 on mobile vs widget POST,
recovery times). Sources: Cloudflare docs, garminconnect & garth issue
trackers, Garmin developer notes.

Answer: the mechanisms that plausibly apply to us, which of our failure kinds
each one would punish, and any published thresholds/decay times.

## Answer

Findings (full write-up, sources and confidence per claim): branch
`research/how-garmin-edge-limits-sign-ins`, file
`.scratch/failing-logins/assets/02-garmin-edge-limits.md` (commit e292f6f).
Garmin publishes nothing; all inferred from Cloudflare docs, response headers
and the garminconnect / garth trackers.

- **Mobile 429 is global** since 2026-03-17 (fingerprint/clientId, any IP) —
  says nothing about our egress; keep skipping mobile. (high)
- **Burned egress = rate limit on the sign-in POST**, not a host-wide IP block
  (embed GET stays 200). Cloudflare rate-limit blocks come in fixed steps
  (… 10 min, 1 h, 1 day); our ~1–2 h / ~1 day recoveries fit a 1 h block
  re-tripped by retries, or the 1-day step. (medium)
- **Cloudflare's login-page template counts failures per IP**: 4/min or
  10/10 min → challenge, 20/h → 1-day block; counters are per data center.
- **Bot protection is on** (`__cf_bm`); whether account-takeover detection
  (baseline of login failures) applies is unknown.
- **A second, Garmin-side (CAS) limiter is plausible**: CAS can throttle failed
  logins per IP or IP+username; garminconnect also checks for account "locked".
- Mapping to our failure kinds: wrong password (MFA page, no code) = a failed
  login on Garmin's side we can't see; repeat sign-in after success = pure
  waste counted by any per-POST limit; many emails per source = volume and a
  stuffing signal, not keyed by stock features; retrying into a block keeps
  the egress over the threshold.
- Suggestions for "Choose the throttle policy" (not decisions): log what a
  burned 429 returns (`Retry-After`, Cloudflare 1015 vs CAS page); a per-egress
  hourly failure budget counting never-completed MFA as failure; stop repeat
  sign-ins after success; consider a 1–2 h egress cooldown.
