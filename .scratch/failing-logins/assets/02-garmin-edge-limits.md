# How Garmin's edge limits sign-ins — research notes

Asset for [02 — How Garmin's edge limits sign-ins](../issues/02-how-garmin-edge-limits-sign-ins.md).
Researched 2026-10-06. Garmin publishes nothing about its sign-in limits, so
everything about *Garmin's* configuration is inferred. The sources below give us
the **menu of mechanisms** (Cloudflare and CAS docs), **what we can see** in
Garmin's responses, and **what the community reports** (garminconnect and garth
trackers). Confidence labels: **High** means stated by a primary source or
observed directly. **Medium** means consistent with several independent reports
or observations. **Low** means plausible inference only.

## TL;DR

1. **The mobile-strategy 429 says nothing about our egress.** Since 2026-03-17
   `/mobile/api/login` has returned 429 for everyone: residential, mobile and
   cloud IPs alike, even on the first attempt after 24 h of silence, and even
   from a browser or curl. garminconnect attributes it to the client
   fingerprint or clientId, not the IP. Our "429 on every attempt from clean
   Hetzner IPs" matches that. Skipping mobile on proxied sign-ins was the right
   call. **High.**
2. **The widget credential POST 429 is a per-source limit scoped to the
   credential POST.** An IP can be "burned" for the POST while the embed GET
   still returns 200. That rules out a host-wide IP block (a WAF IP block or
   bot block would also hit the GET). It fits a **rate limiting rule matched
   on `POST /sso/signin`** with a fixed-duration mitigation. **Medium.**
3. **Cloudflare's mitigation timeouts come in fixed steps:** 10 s, 1, 2, 5 or
   10 min, 1 h, or 1 day. Our observed recovery (about 1–2 h, once about 1 day)
   fits a **1 h block that re-arms while traffic keeps arriving**, or a
   1-day tier. Cloudflare's own credential-stuffing template blocks for 1 day
   after 20 failed logins per hour. **Medium** for the fit; Garmin's actual
   values are unknown.
4. **What gets counted is failures and repeats from one source, not
   "distinct emails" as such.** Cloudflare's stock login rules key on IP and
   count only failed responses. Enterprise Bot Management adds zone-baseline
   **account-takeover detections** (anomalous rise in login failures or login
   attempts) that lower the bot score. Garmin's SSO sets `__cf_bm`, so some bot
   product is active. Whether it is Enterprise Bot Management is unknown.
   **Medium.**
5. **Garmin's SSO is a CAS server** (`x-application-context: casServer:…`).
   Apereo CAS has its own failed-login throttle, keyed per IP or per IP plus
   username, that can answer 423 or 429. A second, origin-side limiter
   independent of Cloudflare is therefore plausible. **Low–Medium.**
6. **Wrong passwords on our widget path are invisible failures.** Garmin
   returns its MFA page for any password, so a wrong password does not show
   up to *us* as an auth error. It still reaches Garmin as a failed credential
   check from our egress (inference: Garmin's backend knows the password was
   wrong, which is why it sends no code). garminconnect also fires an extra
   `POST /sso/verifyMFA/mfaCode` on every email/SMS MFA page it reaches. Each
   "needs MFA, never completed" attempt therefore costs at least two POSTs on
   the limited path. **Medium.**

## Mechanisms that plausibly apply

### A. Cloudflare WAF rate limiting rules (most likely cause of "burned egress")

What the docs say (**High**, primary):

- **Counting keys** ("characteristics"): IP, IP with NAT support, JA3/JA4
  fingerprint, headers, cookies, query, form or JSON body fields, ASN,
  country, and more. "The available characteristics depend on your Cloudflare
  plan." Anything other than IP needs Enterprise Advanced Rate Limiting.
  [WAF rate-limiting parameters](https://developers.cloudflare.com/waf/rate-limiting-rules/parameters/)
- **Period** (counting window): 10 s, 1 min, 2 min, 5 min, 10 min or 1 h.
  Same source.
- **Mitigation timeout** (how long a block lasts): 0 (throttle), 10 s,
  1/2/5/10 min, 1 h, or **1 day**. The two modes are: *fixed duration*
  ("applies the action to all requests during the mitigation period") or
  *throttle* (block only the excess). Same source.
- **Counting on the response**: `counting_expression` can count only, for
  example, `http.response.code in {401 403}`. In other words, only *failed*
  logins count. This needs Business or higher. Same source, plus
  [Rate limiting best practices](https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/).
- **Cloudflare's stock credential-stuffing template**, all rules counted per IP
  on failed logins only:
  - 4 per 1 min → Managed Challenge
  - 10 per 10 min → Managed Challenge
  - 20 per 1 h → **Block for 1 day**

  [Best practices — Protecting against credential stuffing](https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/).
  An older Cloudflare blog example: "rate limits any IP after 5 consecutive
  failed login attempts in a minute for 1 hour".
  [Account takeover protections](https://blog.cloudflare.com/account-takeover-protection/)
- **Default block response is HTTP 429** (customizable on Pro and up), shown
  to the visitor as Cloudflare error 1015.
  [Create rate limiting rule](https://developers.cloudflare.com/waf/rate-limiting-rules/create-zone-dashboard/).
  Since 2026-03 Cloudflare-generated 1015 responses carry
  **`Retry-After: 30`**, unless the rule sets a dynamic value.
  [Changelog 2026-03-12](https://developers.cloudflare.com/changelog/post/2026-03-12-retry-after-header-for-1xxx-errors/)
- **Counters are per data center**: "Counters are not shared across data
  centers, with the exception of data centers associated with the same
  geographical location."
  [Request rate calculation](https://developers.cloudflare.com/waf/rate-limiting-rules/request-rate/).
  Both our egresses run on one box and land in the same Cloudflare location,
  but they have different source IPs, so their per-IP counters are separate.

How this fits our observations (**Medium**):

- **POST blocked while the embed GET returns 200.** That is exactly a rule
  scoped by path and method (`POST /sso/signin`). The stock template's third
  rule is host-wide, so Garmin's setup differs from the template at least
  there.
- **Recovery after about 1–2 h, and once about 1 day.** With a 1 h
  fixed-duration block, recovery in 1–2 h means the IP stayed over threshold
  for a while: users kept retrying into the block, and with a failures-only
  counting expression our 429s may not count, but a fresh failure right after
  release re-arms the block. The about-1-day case matches the 1-day tier. We
  can't tell which tiers Garmin uses.
- **The egress that carried about 2× the attempts and about 5× the
  same-account repeats got burned.** Per-IP counting punishes exactly that
  volume, whoever the account is.

Which failure kinds rule A punishes:

| Failure kind | Punished? | Why |
|---|---|---|
| blocked (429) | Indirectly | Retries during a block keep the source "hot"; with a fixed-duration block they don't extend it, but the first failures after release re-trip it. |
| needs MFA, never completed (wrong password) | **Yes, if counting is failure-based.** | It is a failed credential check. Garmin answers with an MFA page, which is probably a 200, so a `{401 403}` counting expression would *not* see it unless Garmin counts on another signal. Unknown. |
| repeat sign-in right after a success | **Yes, if counting is all-POST.** | Plain request-count rules count successes too. |
| many distinct emails per source | Only through volume | Per-IP rules don't see distinctness. They see more POSTs or more failures. |

### B. Cloudflare Bot Management / Super Bot Fight Mode (bot score, fingerprint)

- `sso.garmin.com` sets **`__cf_bm`** (checked with one GET on 2026-10-06). It
  is set by Bot Management, Bot Fight Mode *or* Super Bot Fight Mode, so this
  doesn't pin the tier. It expires after 30 min of inactivity and carries
  bot-score inputs. **High** that some bot product is on.
  [Cloudflare cookies](https://developers.cloudflare.com/fundamentals/reference/policies-compliances/cloudflare-cookies/)
- garth #217 shows the portal sign-in posting to
  `/cdn-cgi/challenge-platform/.../jsd/oneshot`. That is Cloudflare's
  JavaScript-detections beacon, so the portal flow expects a browser
  executing JS. **Medium.**
  [garth #217](https://github.com/matin/garth/issues/217)
- **Enterprise Bot Management account-takeover detections** build a
  zone-level baseline and flag an *anomalous rise in login failures*
  (brute force) or *in login attempts* (credential stuffing). This feeds the
  bot score. No windows or decay times are published.
  [Per-customer bot defenses](https://blog.cloudflare.com/per-customer-bot-defenses/),
  [Stop account takeover attacks](https://developers.cloudflare.com/use-cases/solutions/stop-account-takeover-attacks/).
  **High** that the feature exists; **unknown** whether Garmin has it.
- **Fingerprint keying**: JA3/JA4 are rate-limit characteristics and bot
  signals. garminconnect states that "different TLS fingerprints land in
  different Cloudflare rate-limit buckets", rotates curl_cffi impersonations
  because of it, and says mobile 429 "can be triggered by the client
  fingerprint, not only the IP". It also waits 3–8 s between the widget GET
  and the POST, and 10–20 s on the portal path, because "Cloudflare flags
  rapid GET→POST sequences as bot-like".
  [garminconnect client.py](https://github.com/cyberjunky/python-garminconnect/blob/master/garminconnect/client.py).
  **Medium**: the maintainer's empirical belief, not a Garmin statement.
  Implication: every one of our sign-ins presents the **same** curl_cffi
  "chrome" fingerprint from a datacenter ASN. If Garmin counts by JA3/JA4 or
  IP+JA4, all our egresses could share one bucket. The per-IP burn pattern
  (one egress burned, the other fine) says the binding limit right now is
  per-IP. **Medium.**

Which failure kinds B punishes: mostly **volume of login attempts and failures
from a datacenter-looking client**, so many distinct emails, wrong passwords
and repeats all add up. A low bot score typically yields a challenge or 403,
not a 429. Our "blocked" 429s are therefore more likely rule A (or C), and B
shows up as the portal path's CAPTCHA and 403 responses. **Low–Medium.**

### C. Garmin's CAS server throttle (origin side)

- Response header `x-application-context: casServer:cloud,prod,…` (one GET,
  2026-10-06) shows the SSO backend is a CAS server. garth #217 also notes the
  flow is CAS (`serviceTicketId`, `authType: CAS`). **High** that it's CAS;
  **Low** that it is Apereo CAS with throttling enabled.
- Apereo CAS throttling counts *failed* logins either "from the same IP
  address" or "against a particular user from the same IP address". It is
  configured as threshold and range-seconds (a rate), plus a throttle window,
  and released by window, by scheduler (default every 2 min) or by decay once
  the rate falls. Throttled requests get 423 or 429. Defaults are off.
  [Apereo CAS — authentication throttling](https://apereo.github.io/cas/development/authentication/Configuring-Authentication-Throttling-Failure.html).
  **Medium** for what CAS can do (doc summary; status-code detail not
  verified in source).
- The widget page titles that garminconnect checks include **"locked"** (as
  well as "invalid" and "incorrect"). That points to a per-account lockout on
  Garmin's side, separate from IP limits. Garmin has a "Your account is
  temporarily locked" message (forum, no duration given).
  [garminconnect client.py](https://github.com/cyberjunky/python-garminconnect/blob/master/garminconnect/client.py),
  [Garmin forum](https://forums.garmin.com/apps-software/mobile-apps-web/f/garmin-connect-web/110034/your-account-is-temporarily-locked-restore-your-password).
  **Medium** that it exists; no threshold known.

Which failure kinds C punishes: **wrong passwords**, per IP and per IP plus
user. That matters to us: a single user's typo loop from our egress counts
against the egress in the IP mode. An account lockout would hurt that user,
not the egress.

### D. Endpoint- or clientId-wide blocking (the mobile path)

- garth #217 (opened 2026-03-19, still open): 429 on `/mobile/api/login`
  "every time … even with a fresh IP address", from residential, mobile and
  cloud IPs in several countries, "Waiting 24+ hours with zero requests — 429
  on the very first attempt".
  [garth #217](https://github.com/matin/garth/issues/217)
- garminconnect #386 retest logs, 2026-07 and 2026-08: `mobile+cffi → 429`,
  `mobile+requests → 429`, and widget reached MFA on the same run.
  [garminconnect #386](https://github.com/cyberjunky/python-garminconnect/issues/386)
- garminconnect describes the widget as the HTML form flow that "bypasses
  clientId-based rate limits".
  [client.py docstring](https://github.com/cyberjunky/python-garminconnect/blob/master/garminconnect/client.py)
- **Conclusion (High):** mobile 429 is a standing block on that client or
  endpoint, independent of our IP or behaviour. It says nothing about egress
  health. Sending mobile attempts anyway only adds POST volume, whose effect
  on IP reputation is unknown. Keep skipping them.

## Community reports on recovery times

All are anecdotes; **Low–Medium** individually.

- garminconnect maintainer, on a widget-era 429 (#350, 2026-04): "you are
  really blocked.. try again in an hour, and if you made it really bad
  longer". [#350](https://github.com/cyberjunky/python-garminconnect/issues/350)
- garminconnect #213 (2024): a user who hit the login limit by re-running a
  script: "I think the block is removed after 1 hour probably (I tried 1 day
  later)". The maintainer's fix was to reuse stored tokens and never log in
  again with the password.
  [#213](https://github.com/cyberjunky/python-garminconnect/issues/213)
- garth discussion #60: the maintainer believes the 429 is per IP, and that
  shared IPs (household, cloud) pool their counts. Switching to mobile data
  helped. No duration is given.
  [garth #60](https://github.com/matin/garth/discussions/60)
- garth #143 (2025-10): `429 … sso/signin` (the widget path). Fixed by the
  reporter with rotating proxies. The maintainer: it "shouldn't be an issue if
  you're only auth'ing your own user, saving the token and reusing" it.
  [garth #143](https://github.com/matin/garth/issues/143)
- Garmin FIT-SDK forum, around 2026-05: a 429 on all strategies "for 48+
  hours", believed to be "account level, not IP" (tried home Wi-Fi and a
  hotspot). It coincides with the global mobile block (D) and portal
  challenges, so the account-level reading is weak.
  [Garmin forum](https://forums.garmin.com/developer/fit-sdk/f/discussion/435087/persistent-429-on-api-login-account-blocked-for-48-hours)
- garminconnect #337 and garth #217: the same code works from home but not
  from Azure, DigitalOcean, AWS, Railway or GitHub Actions. Several people
  conclude datacenter ranges are treated more strictly. **Medium**: consistent
  across many reporters.
  [#337](https://github.com/cyberjunky/python-garminconnect/issues/337),
  [garth #217](https://github.com/matin/garth/issues/217)

Our own recovery time (about 1–2 h, once about 1 day idle) agrees with the
"about an hour, longer if bad" folklore. It also fits Cloudflare's 1 h and
1 day mitigation tiers.

## Wrong password on MFA accounts: what the sources add

- Our observation: a wrong password returns the MFA page and no code is sent.
  A nonexistent account also returns the MFA page.
- garminconnect #386 and the current code: an email or SMS MFA page reached
  through the widget does not guarantee a code was sent. garminconnect now
  explicitly POSTs `/sso/verifyMFA/mfaCode` (the "request a new code"
  endpoint) when the page shows no `codeSentTo`. That POST can itself return
  429. [#386](https://github.com/cyberjunky/python-garminconnect/issues/386),
  [client.py `_widget_request_mfa_code`](https://github.com/cyberjunky/python-garminconnect/blob/master/garminconnect/client.py).
  Note: our lock pins garminconnect 0.3.6, which predates this; check what
  our version does before counting POSTs.
- Implication (**Medium**): Garmin hides wrong passwords from the client,
  which is anti-enumeration behaviour. Server side it is still a failed
  credential check from our IP. Any failure-counting limiter (A with a
  suitable counting expression, B's login-failure anomaly, C) sees it. We
  can't, until the user gives up at the MFA page.

## Mapping to our failure kinds

| Failure kind | Mechanisms that would punish it | Confidence |
|---|---|---|
| **blocked** (real 429 on the widget POST) | Retrying into an active block keeps the source over threshold: A re-arms after release, C's decay never starts. Every retry is a full GET→GET→POST round. | Medium |
| **needs MFA, never completed** (mostly wrong passwords) | A if counting is failure-based (only if Garmin counts its MFA-page response as a failure, unknown), B (login-failure anomaly), C per IP and per IP plus user, plus a possible per-account lock. Extra MFA-code POST per attempt. | Medium |
| **repeat sign-in right after a success** | A if counting is all-POST (likely for a plain volume rule). B (login-attempt anomaly). Not C (successes don't count). Pure waste: the token from the first success is valid. | Medium |
| **many distinct emails per source** | B (credential-stuffing signature: a datacenter IP with many accounts and many failures). A and C only through the volume or failures it brings. No public Cloudflare feature keys on "distinct usernames per IP" except Enterprise Advanced Rate Limiting with a body-field characteristic, and nothing shows Garmin uses that. | Low–Medium |

## Published thresholds and decay: what exists

- **Garmin**: none published. **High** (absence after search).
- **Cloudflare template** (Garmin's actual values unknown):
  - per IP, failed logins only: 4 per min → challenge; 10 per 10 min →
    challenge; 20 per h → block for 1 day.
  - blog variant: 5 failures per min → block for 1 h.
  - mitigation timeout menu: up to 1 day.
  - 1015 `Retry-After` defaults to 30 s, but that is the header value, not
    necessarily the real block length.
- **Apereo CAS**: no defaults (disabled until configured); its docs call
  "more than 1 per 3 seconds" indicative of automation.

## Suggestions for the decision ticket (03)

These are not decisions.

- **Read the 429 itself.** Log (without credentials) whether the widget POST
  429 carries `Retry-After`, a Cloudflare 1015 body or a `cf-ray`-only page,
  versus a CAS-style page. That tells A from C, and `Retry-After` may give the
  real block length for free. Cheap and decisive.
- **Budget failures per egress per hour, not just attempts.** That is the
  unit the public templates count (failed logins per IP per 1 min, 10 min and
  1 h). Treat "needs MFA never completed" as a failure.
- **Stop "repeat right after success" at our side.** It is never needed and
  plain volume rules count it.
- **Throttle at about 1–2 h after a block, not 30 min,** if the 429 doesn't
  say otherwise. Retrying into a 1 h block wastes the egress.

## Sources

Primary (Cloudflare):
- https://developers.cloudflare.com/waf/rate-limiting-rules/parameters/
- https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/
- https://developers.cloudflare.com/waf/rate-limiting-rules/request-rate/
- https://developers.cloudflare.com/waf/rate-limiting-rules/create-zone-dashboard/
- https://developers.cloudflare.com/waf/detections/leaked-credentials/ —
  leaked credential checks (`cf.waf.credential_check.*`, the
  `Exposed-Credential-Check` header). Password-leaked is on all plans,
  username+password on Pro and up. Usable in rate limiting rules, so
  breached-credential pairs could face a lower limit. Can't tell whether
  Garmin uses it.
- https://developers.cloudflare.com/changelog/post/2026-03-12-retry-after-header-for-1xxx-errors/
- https://developers.cloudflare.com/fundamentals/reference/policies-compliances/cloudflare-cookies/
- https://developers.cloudflare.com/use-cases/solutions/stop-account-takeover-attacks/
- https://blog.cloudflare.com/per-customer-bot-defenses/
- https://blog.cloudflare.com/account-takeover-protection/

Primary (CAS):
- https://apereo.github.io/cas/development/authentication/Configuring-Authentication-Throttling-Failure.html

Library source and issue trackers:
- https://github.com/cyberjunky/python-garminconnect/blob/master/garminconnect/client.py
- https://github.com/cyberjunky/python-garminconnect/issues/213
- https://github.com/cyberjunky/python-garminconnect/issues/337
- https://github.com/cyberjunky/python-garminconnect/issues/344
- https://github.com/cyberjunky/python-garminconnect/issues/350
- https://github.com/cyberjunky/python-garminconnect/issues/386
- https://github.com/matin/garth/issues/217
- https://github.com/matin/garth/issues/143
- https://github.com/matin/garth/discussions/60
- https://forums.garmin.com/developer/fit-sdk/f/discussion/435087/persistent-429-on-api-login-account-blocked-for-48-hours

Direct observation: one unauthenticated GET of `sso.garmin.com/sso/embed`
on 2026-10-06 returned `server: cloudflare`, a `__cf_bm` cookie (30-min
lifetime), a `__cflb` load-balancer cookie and
`x-application-context: casServer:cloud,prod,…`.
