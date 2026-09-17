# 12 — Garmin SSO rate-limits our Railway egress IP: fresh sign-ins ~25–75 % failing

Type: incident (upstream/infra)
Status: ready-for-human (mitigation decision = spend)

## Symptom

Since 2026-09-13, escalating 09-14 → 09-16, still ongoing 09-17: fresh
Garmin sign-ins (and re-logins) on the authorize form fail in waves.
Existing users are unaffected — workers log in from token files and
`connectapi` (connect.garmin.com) is not blocked; only the SSO portal is.

Daily numbers: 509× `login-start-timeout` 09-13→09-17 (peak 337 on 09-16),
success rate ~25 % at the worst; the daily triage counted 1204 error rows
in its 09-16/17 window. Two accounts got stuck in the stale-token +
blocked-re-login combination (jpbos***, valer***, both 09-15).

## Root cause (verified 2026-09-17)

Cloudflare in front of `sso.garmin.com` rate-limits **our egress IP**
(152.55.184.37 — a shared Railway address, so other tenants' traffic
counts against it too). Proof: the same browser-impersonated request
(`curl_cffi`, `impersonate="chrome"` — exactly what garminconnect uses)
returns **429 from the production container and 200 from a residential
IP**. A `railway redeploy` did not change the egress IP (sticky per
region/env).

Not a gateway bug and not a library version issue: garminconnect 0.3.13–15
release notes carry no login fix, upstream garmin_mcp hasn't bumped it.
The triage's "unhandled exception in the login library" is garminconnect's
own error-level log (traceback ending
`GarminConnectTooManyRequestsError: All login strategies rate limited
(429)`); the gateway catches it and maps it to `reason=blocked` correctly.

## Mitigation options (operator decision)

1. **Wait** — 429 is a decaying rate limit; the wave rate is falling on
   09-17 (162/h morning → 80/h). Retries do get through. Zero cost, but
   the shared-IP exposure recurs as we grow (~+20 accounts/day).
2. **Railway Pro static outbound IP** — dedicated, unshared IP with clean
   reputation. Plan upgrade cost; the durable fix. (Memory limit today is
   6 GB on Hobby's 8 GB cap, so the Pro headroom eventually helps capacity
   too.)
3. **Tiny SSO-only proxy on a clean IP** (~$3–5/mo VPS) — wire only the
   sign-in path through it in `adapters/garmin/login.py`; workers, WHOOP,
   PostHog, S3 stay direct. Cheaper, one more box to keep alive.
4. **Circuit-breaker on the sign-in path** (complements any of the above)
   — while the portal is blocked, fail fast with the "blocked" message
   instead of burning 30 s of strategies per attempt; fewer of our own
   requests hammer the rate limiter.

## Related

- The `mar***` initialize/handshake timeouts the triage flagged are the
  separate known pattern of per-account Garmin slowness (Cloudflare 504s
  on connectapi), self-healing, watched informally.
- Issue 11 (getCustomFoods) unaffected and still quiet.
