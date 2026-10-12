# Build, deploy and measure the throttle policy

Type: task
Status: claimed
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
