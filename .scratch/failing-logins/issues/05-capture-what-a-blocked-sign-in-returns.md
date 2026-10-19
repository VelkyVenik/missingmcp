# Capture what a blocked sign-in returns

Type: task
Status: resolved
Blocked by: —

## Question

When a sign-in comes back blocked, what exactly did Garmin's edge answer?
Log (no credentials, no bodies beyond a short classifier) the HTTP status of
the failing widget/portal POST, any `Retry-After` header, and whether the body
is Cloudflare's rate-limit page (error 1015) or a CAS page. This tells
"Choose the throttle policy" whether the limiter is Cloudflare or CAS and how
long its block really lasts (sets the egress cooldown). Small code change via
PR; garminconnect's exceptions may need their response inspected.

## Comments

- 2026-10-12 — **Partly answered from existing logs** (garminconnect's own
  warning lines, 2026-10-11 08:35 → 10-12 14:03 UTC, 0.3.2 era). Of 40 sign-ins
  reported `blocked`: **0** had a widget 429. 38 had the widget POST answered
  with the sign-in page itself (title "GARMIN Authentication Application" — the
  form handed back, i.e. Garmin didn't accept the credentials, or an email-MFA
  page 0.3.2 couldn't recognize), 2 a "Set Password" page; in all 40 the
  fallback portal strategies then hit a Cloudflare bot challenge (403) and the
  library reported "exhausted" → we said `blocked`. Mobile strategies added 390
  pointless 429s (skip ignored by 0.3.2). So most "blocked" was never Garmin's
  edge limiting our egress. Fix (PR after #46): portal skipped when proxied;
  the widget page title is logged as `page` on `garmin-login-attempt` and
  mapped — sign-in form → `auth`, "Set Password" → `password_reset`; neither
  cools the account nor counts against the egress. HTTP status / Retry-After
  of a real widget 429 still unlogged — revisit only if real 429s show up.

## Answer

Answered from garminconnect's own log lines (comment 2026-10-12): what we
called "blocked" was almost never Garmin's edge limiting an egress — it was
the sign-in form handed back (wrong credentials or an email-MFA page 0.3.2
misread), a "Set Password" page, and the portal fallback's Cloudflare 403.
After PR #47 (page title logged as `page`, mapped to `auth` /
`password_reset`) the measurement saw **0** real blocks in ~700 attempts, so
there was no 429 left to capture. HTTP status / Retry-After of a real widget
429 stays unlogged; revisit only if `blocked` reappears.
