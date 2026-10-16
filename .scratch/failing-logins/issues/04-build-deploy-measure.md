# Build, deploy and measure the throttle policy

Type: task
Status: resolved
Blocked by: 03

## Question

Implement the chosen policy (feature branch + PR, tests), deploy, and run the
7-day measurement against the destination criteria. Record what shipped, the
before/after numbers, and whether the destination is reached.

## Comments

- 2026-10-08 — **Shipped**: PR #44 (merge 539e672), deployed 2026-10-08T17:22Z.
  Egresses: 3128 / 3129 / 3130 / 3131 + direct. Measurement window runs 7 days
  from the deploy; daily check of egress-health, login-breaker-* by scope, and
  sign-in outcomes against the destination criteria.
- 2026-10-08 — **Baseline, 24 h before the throttle** (2026-10-07 17:22 →
  2026-10-08 17:22 UTC, 4 egresses + direct): 194 attempts from 138 emails,
  72 % through / **28 % blocked**, 132 tokens issued. Per egress through-rate:
  3128 78 %, 3129 78 %, 3130 68 %, 3131 67 %; direct unused. Longest run of
  egress-health windows with 0 through: 1 window (~30 min) on each proxy.
  Rejects: 46 × account cooldown, 0 × egress (no full rejection). Egress
  trips: 3; account cooldowns: 51. Same-email attempt < 2 min after the
  previous one: 27 (8 of them right after an `ok`) — the traffic the
  in-flight slot and post-connect hold target.
- 2026-10-09 — **Day 1** (2026-10-08 17:22 → 2026-10-09 07:48 UTC, ~14.5 h,
  mostly night): 67 attempts from 55 emails, 72 % through / **28 % blocked**
  (baseline 28 %; first post-proxy days 42 %), 44 tokens. Per egress
  through-rate: 3128 71 %, 3129 83 %, 3130 74 %, 3131 58 %; direct unused.
  Longest 0-through run: 60 min (3131, from 07:05) — under the 2 h criterion.
  Rejects: 27 × account, **5 × in_flight, 4 × post_success**, 0 × egress (no
  full rejection). Egress trips: 0 (baseline 3); account cooldowns 19.
  Same-email attempts < 2 min apart reaching Garmin: **0** (baseline 27) —
  the in-flight slot and post-connect hold catch them before Garmin.
- 2026-10-10 — **Day 2** (2026-10-09 07:56 → 2026-10-10 07:39 UTC, full day):
  151 attempts from 112 emails, 68 % through / **32 % blocked** (baseline
  28 %, day 1 28 %, first post-proxy days 42 %); 88 % of emails got through;
  1.35 attempts per email (baseline 1.41); 100 tokens. Blocked: 30 on a first
  try, 18 on a repeat (all ≥ 2 min apart — 0 same-email attempts < 2 min
  reached Garmin). Per egress through-rate: 3128 71 %, 3129 70 %, 3130 70 %,
  3131 61 %; direct unused. Longest 0-through run: 60 min (3130, 3131) — under
  2 h. Rejects: 41 × account, 9 × in_flight, 4 × post_success, **0 × egress**.
  Egress trips: 6, all at the first rest step (30 min) — three of them within
  33 min in the morning peak (09:41–10:14), so a peak can close 3 of 4
  egresses at once; 3130 carried it. Account cooldowns: 42.
- 2026-10-11 — **Day 3** (2026-10-10 07:39 → 2026-10-11 07:21 UTC, Saturday —
  lower traffic): 98 attempts from 83 emails, 73 % through / **26.5 % blocked**
  (baseline 28 %, day 1 28 %, day 2 32 %, pre-throttle 42 %); 83 % of emails
  got through; 1.18 attempts per email (day 2 1.35, baseline 1.41); 72 tokens.
  Blocked: 22 on a first try, 4 on a repeat. One same-email attempt < 2 min
  reached Garmin (needs_mfa → needs_mfa, 44 s — no hold applies after an
  unfinished MFA; harmless). Per egress through-rate: 3128 80 %, 3129 83 %,
  3130 64 %, 3131 69 %; direct unused. Longest 0-through run: 30 min (one
  window, every egress) — under 2 h. Rejects: 24 × account, 6 × in_flight,
  **0 × egress**, 0 × post_success. **Egress trips: 0**. Account cooldowns: 26.
- 2026-10-12 — **Day 4** (2026-10-11 07:21 → 2026-10-12 07:36 UTC, Sunday):
  150 attempts from 116 emails, 80 % through / **20 % blocked** — lowest so
  far (pre-throttle 42 %, baseline 28 %, days 1–3: 28 / 32 / 26.5 %); 91 % of
  emails got through; 1.29 attempts per email; 117 tokens. Blocked: 20 on a
  first try, 10 on a repeat; 0 same-email attempts < 2 min reached Garmin.
  Per egress through-rate: 3128 81 %, 3129 93 %, 3130 67 %, 3131 83 %;
  direct unused. 3130 again carries the most traffic (52 of 150) and the most
  blocks (17) — third day running it is the weakest egress. Longest 0-through
  run: 30 min (3128, 3130, 3131) — under 2 h. Rejects: 29 × account,
  7 × in_flight, 2 × post_success, **0 × egress**. Egress trips: 2, both at the
  first rest step (30 min), an hour apart (3128 afternoon, 3130 afternoon) —
  never more than one egress resting at a time. Account cooldowns: 28.
- 2026-10-12 — **Measurement break.** Reviewing an unrelated PR showed prod ran
  `garminconnect` **0.3.2**, not the 0.3.6 in `uv.lock`: the worker's
  `garmin-mcp` pins `==0.3.2` and was installed into the same site-packages
  after the gateway. 0.3.2 has no `skip_strategies` — so every proxied sign-in
  still ran both mobile strategies (two 429s per attempt on our egress) — and
  detects MFA only by "MFA" in the page title, so email-MFA pages ("GARMIN
  Authentication Application") fell through as "unexpected title" and could end
  as `blocked`. Fix: gateway pinned to 0.3.17, worker in its own venv. Days 1–4
  above are the 0.3.2 era; compare the days after the deploy separately.
- 2026-10-12 — **0.3.17 deployed** at 14:14 UTC (PR #46, e2ab9f2; verified in
  the container: gateway 0.3.17, worker venv 0.3.2). Days measured from here on
  are the "after" series.
- 2026-10-12 — **Second break (PR after #46):** sign-in form handed back is now
  `auth` and "Set Password" is `password_reset` (were `blocked`), portal
  strategies skipped through the proxy. From this deploy on, `blocked` means
  Garmin's edge really refused; compare blocked share only within the new
  series, and track `auth` separately (see ticket 05's comment).
- 2026-10-12 — PR #47 deployed at 14:25 UTC (b154721). The "new series" for
  the blocked share starts here.
- 2026-10-13 — **Day 5, first day of the new series** (2026-10-12 14:25 →
  2026-10-13 05:29 UTC, ~15 h, includes PR #48 deploy at 20:14): 109 attempts
  from 87 emails — 73 ok, 15 needs_mfa, 21 auth (wrong credentials, all the
  sign-in form handed back), 0 password_reset, **0 blocked**. 98 % of emails
  got through (85/87); 1.25 attempts per email; 84 tokens. 17 emails hit
  `auth`, 15 of them signed in later. Per egress through-rate (non-`auth`
  attempts all got through): 3128 84 %, 3129 79 %, 3130 87 %, 3131 72 %;
  direct unused. Peak load per egress: 5–8 attempts/hour. Longest 0-through
  run: none. Rejects: 0; breaker opens: 0 (account and egress). Worker side:
  one `worker-login-timeout` (startup budget 20 s), 0 `worker-login-rejected`.
- 2026-10-13 — **Day 5, full 24 h** (2026-10-12 07:36 → 2026-10-13 07:35 UTC;
  supersedes the partial entry above): 223 attempts from 172 emails — 134 ok,
  30 needs_mfa, 45 auth, 0 password_reset, 14 blocked (6.3 %). All 14 blocked
  fell before 14:25 UTC (old era: 14 of 54 = 26 %, in line with days 3–4);
  **new series 14:25 → 07:35: 169 attempts / 127 emails, 0 blocked**, 94 % of
  emails through, 45 auth (33 emails, 26 later signed in). Whole day: 93 % of
  emails through, 1.30 attempts per email, 162 tokens. Per egress through-rate
  (auth counted as not through): 3128 67 %, 3129 78 %, 3130 83 %, 3131 66 %;
  direct unused; peak 5–12 attempts/hour per egress. Longest 0-through run:
  30 min (3129, 3130, 3131, all in the old era) — under 2 h. Rejects:
  23 × account, 2 × in_flight, **0 × egress**. Opens: 13 × account,
  1 × egress (3131, 14:24, rest 1800 s, last minute of the old era). The
  morning peak (05–08 UTC, 63 attempts) passed with 0 blocked.
- 2026-10-14 — **Day 6** (2026-10-13 07:35 → 2026-10-14 07:35 UTC; recorded
  2026-10-15, the 10-14 run did not happen): 329 attempts from 226 emails —
  176 ok, 55 needs_mfa, 97 auth, 1 password_reset, **0 blocked** (day 5 new
  series 0 %, pre-throttle 42 %). 96 % of emails got through; 1.46 attempts
  per email; 226 tokens. 58 emails hit `auth`, 48 signed in later. Per egress
  through-rate (auth counted as not through): 3128 66 %, 3129 76 %, 3130
  71 %, 3131 68 %; direct unused; peak 6–16 attempts/hour per egress, busiest
  hour 40 attempts overall. Longest 0-through run: none. Rejects: 5 ×
  post_success, **0 × egress**, 0 × account, 0 × in_flight. Opens: 0.
- 2026-10-15 — **Day 7** (2026-10-14 07:35 → 2026-10-15 07:52 UTC): 201
  attempts from 146 emails — 108 ok, 40 needs_mfa, 52 auth, 1
  password_reset, **0 blocked**. 94 % of emails got through; 1.38 attempts
  per email; 146 tokens. 39 emails hit `auth`, 30 signed in later. Per egress
  through-rate: 3128 79 %, 3129 80 %, 3130 72 %, 3131 67 %; direct unused;
  peak 6–8 attempts/hour per egress. Longest 0-through run: none. Rejects:
  2 × post_success, **0 × egress**. Opens: 0. Worker side: 6
  `worker-login-rejected` for 2 accounts (stale tokens → re-auth 401, the
  `/readyz` self-heal; one re-signed within a minute), 0 new
  `worker-login-timeout`.
- 2026-10-15 — **7-day summary against the destination** (2026-10-08 17:22 →
  2026-10-15 07:52 UTC). Blocked share by day: 28 / 32 / 26.5 / 20 % (days
  1–4, throttle on top of garminconnect 0.3.2), then 6.3 % on day 5 (all
  before the 14:25 fix) and **0 % from 2026-10-12 14:25 on** (~700 attempts,
  days 5–7). Pre-throttle 42 %, baseline 28 %. Criterion 1 — **met**: longest
  run of egress-health windows with 0 through was 60 min (day 1), never
  > 2 h; **0 egress rejects in all 7 days** (no full rejection); egress trips
  only at the first rest step (30 min); at most 3 of 4 resting at once
  (day 2 morning peak), never all. Criterion 2 —
  **met** since the fix: of the emails that hit wrong credentials and later
  got in, 96 of 104 did so within 5 min of their last wrong attempt (median
  50 s); the gateway rejected none of them in between (wrong credentials no
  longer cool the account) — the slower ones are the user's own pace. 25
  emails never got past wrong credentials (no lockout on our side). What
  moved the numbers: the throttle (PR #44) removed same-email repeats reaching
  Garmin, but the drop to 0 blocked came from PR #46 (gateway on
  garminconnect 0.3.17, proxied sign-ins skip the mobile strategies) and
  PR #47 (portal skipped behind the proxy; the sign-in form handed back is
  `auth`, not `blocked`). Recommendation: resolve this ticket and close the
  map; the "Abuse response" fog never materialized (no source driving many
  emails) and can move to Out of scope. Follow-ups outside this map: egress
  count (peak ≤ 16 attempts/hour per egress — operator infra track) and the
  proxy password rotation.
- 2026-10-16 — **Day 8, post-resolution check** (2026-10-15 07:37 → 2026-10-16
  07:37 UTC): 145 attempts — 81 ok, 25 needs_mfa, 29 auth, **10 blocked
  (6.9 %)**; day 7 was 0 %, pre-throttle 42 %. Per egress through-rate: 3128
  87 %, 3129 69 %, 3130 68 %, 3131 73 %; direct unused; 96 tokens. All 10
  blocks fall in the last hour (06:34–07:35 UTC, 6 accounts, no SSO page
  title) — the first blocks since 2026-10-12 14:25. Egress breakers tripped at
  the first rest step (1800 s): 3129 07:16, 3130 07:21, 3131 07:35 — **3 of 4
  resting at once**, 3128 still taking sign-ins (ok at 07:03). Opens: account
  7 × 300 s (3129 3, 3130 3, 3131 1), egress 3 × 1800 s. Rejects: account 11,
  post_success 10, in_flight 0, **egress 0** (no full rejection). Longest
  0-through run: 2 windows (≤ 60 min) on 3129 and 3130 — criterion (≤ 2 h)
  still met. Episode still open at the time of writing; the gateway's
  stateless-worker deploy (2026-10-15 19:46, PR #49) does not touch the
  sign-in path.

## Answer

Destination reached (resolved 2026-10-15). Shipped: the throttle policy
(PR #44) plus two fixes the measurement surfaced — the gateway on
garminconnect 0.3.17 with the worker in its own venv (PR #46) and wrong
credentials / "Set Password" told apart from blocks, portal skipped behind the
proxy (PR #47). Over the 7 days (2026-10-08 17:22 → 2026-10-15 07:52 UTC):
no egress without a sign-in getting through for more than 60 min, 0 egress
rejects (no full rejection), and since 2026-10-12 14:25 0 % blocked
(~700 attempts; pre-throttle 42 %) with 96 of 104 users who fixed wrong
credentials getting in within 5 min and none rejected by us in between.
Detail in the 2026-10-15 summary comment above.
