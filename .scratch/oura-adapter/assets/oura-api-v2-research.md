# Oura API v2 — primary-source research (researched 2026-07-17)

Sources are Oura-owned only: the v2 API reference at https://cloud.ouraring.com/v2/docs (rendered from the
OpenAPI document `https://cloud.ouraring.com/v2/static/json/openapi-1.35.json` — quoted below as "v2 docs"),
the authentication page https://cloud.ouraring.com/docs/authentication, the error-handling page
https://cloud.ouraring.com/docs/error-handling, the Oura Member Care article
https://support.ouraring.com/hc/en-us/articles/4415266939155-The-Oura-API, plus two live-endpoint probes
made on 2026-07-17 (marked "verified empirically"). The partner-support article
(https://partnersupport.ouraring.com/hc/en-us/articles/20949682312211-Intro-to-the-Oura-API) is
Cloudflare-gated and could not be read; nothing below relies on it.

## TL;DR

1. **Auth**: OAuth2 (authorization-code) is the only option — "personal access tokens were deprecated in December 2025 and are no longer available for use" (v2 docs). 8 scopes; `daily` covers sleep/readiness/activity daily summaries, `heartrate` time-series HR, `workout` workouts.
2. **Tokens**: refresh token **rotates WHOOP-style** — "single-use, meaning it is invalidated after being used"; each refresh returns a new pair. **No `offline`-style scope needed** — refresh tokens come by default in the server-side flow. Access-token TTL: read `expires_in` (docs self-contradict: "typically 24 hours" vs "typically last 30 days").
3. **Registration**: needs an Oura account (app pages sit behind sign-in); docs don't say a ring/membership is required to register. Cap: "API Applications are limited to **10** users before requiring approval from Oura. There is no limit once an application is approved" — the approval process itself is undocumented (contact channel: api-support@ouraring.com). Redirect URIs: exact-match whitelist, multiple allowed; https/localhost rules are **not documented**.
4. **Endpoints**: 19 `GET /v2/usercollection/*` data routes (list below); pagination = `start_date`/`end_date` + `next_token` (heartrate: `start_datetime`/`end_datetime`); the data API is **fully read-only** — the only non-GET routes are webhook-subscription management. Gen3+ users **without an active membership get no API data** (403).
5. **Identity**: `personal_info` returns `id, age, weight, height, biological_sex, email` — **email is available but nullable and requires the `email` scope**, which the user can decline at consent; `id` is the only always-available field.
6. **Rate limits & sandbox**: "5000 requests in a 5 minute period", plus a newer two-layer regime (per-access-token + per-application aggregate) with 429 + `Retry-After`/`X-RateLimit-*` headers; a full **sandbox** (`/v2/sandbox/usercollection/*`) serves fake data for every data type without an Oura account (any string in the `Authorization` header works — verified empirically).

---

## 1. Auth options: OAuth2 vs Personal Access Tokens

- **PATs are dead.** The v2 docs (Authentication section of the API overview) state: "Please note that
  personal access tokens were deprecated in December 2025 and are no longer available for use."
  (https://cloud.ouraring.com/v2/docs, `openapi-1.35.json` → `info.description`). The current
  authentication page (https://cloud.ouraring.com/docs/authentication) documents **only** OAuth2 — the PAT
  section that used to exist is gone. The old PAT console URL `https://cloud.ouraring.com/personal-access-tokens`
  still resolves but just 302s to sign-in (verified empirically 2026-07-17); the docs' statement that PATs
  are "no longer available for use" is the authoritative word.
- **OAuth2**: standard authorization-code flow (plus a client-side/implicit flow, see §2).
  - Authorize: `https://cloud.ouraring.com/oauth/authorize`
  - Token: `https://api.ouraring.com/oauth/token`
  (https://cloud.ouraring.com/docs/authentication)
- **Scopes** (all 8, from https://cloud.ouraring.com/docs/authentication):
  | scope | covers |
  |---|---|
  | `email` | "Email address of the user" |
  | `personal` | "Personal information (gender, age, height, weight)" |
  | `daily` | "Daily summaries of **sleep, activity and readiness**" |
  | `heartrate` | "Time series **heart rate** for Gen 3 users" |
  | `workout` | "Summaries for auto-detected and user entered **workouts**" |
  | `tag` | "User entered tags" |
  | `session` | "Guided and unguided sessions in the Oura app" |
  | `spo2` / `spo2Daily` (see below) | "Daily SpO2 Average recorded during sleep" |
- **Scope-name discrepancy to verify at implementation time**: the authentication page's HTML marks the
  scope as `<code>spo2</code>` (with the description apparently starting "Daily SpO2 Average…"), while the
  OpenAPI document's OAuth2 security scheme keys it **`spo2Daily`** with description "SpO2 Average recorded
  during sleep" (`openapi-1.35.json` → `components.securitySchemes.OAuth2.flows.authorizationCode.scopes`).
  One of the two pages has a markup/typo bug. Test the actual string against `/oauth/authorize` before
  hard-coding.
- **No published endpoint→scope matrix.** The scope list above is the only mapping the docs give. A 403 for
  missing scopes tells you what you lacked: "Your token must have the scopes listed as part of the detail
  of the error" (https://cloud.ouraring.com/docs/error-handling). Leaving `scope` blank on the authorize
  request "will request **all** available scopes" (https://cloud.ouraring.com/docs/authentication).
- **Users can partially consent**: "When an Oura user authorizes access to your application, she can enable
  and disable scopes based on her preferences" — and the redirect back carries the **actually granted**
  scopes: "Note that the space-separated list of scopes may be different than the scopes that were
  requested." (https://cloud.ouraring.com/docs/authentication)

## 2. Token semantics (OAuth path)

- **Refresh token rotation — WHOOP-style, confirmed.**
  - Token-exchange and refresh responses both include a `refresh_token` described as: "A refresh token that
    can be used to acquire a new access token (and refresh token) after this access token expires. **The
    refresh token is single-use, meaning it is invalidated after being used.**"
    (https://cloud.ouraring.com/docs/authentication, "Exchange Code For Access Token" and "Get New Access
    Token Using The Refresh Token")
  - Error-handling page, "Token Already Used Or Revoked": "If you are trying to use a refresh token, note
    that **it can only be used once**." (https://cloud.ouraring.com/docs/error-handling)
  - Consequence for the gateway: the same serialize-per-account + persist-rotated-blob-before-use discipline
    as `WhoopApi.ensure_fresh` is required.
- **No special scope for refresh tokens.** Unlike WHOOP's `offline`, the server-side flow returns
  `refresh_token` unconditionally; neither the authentication page nor the v2 docs mention any scope or
  parameter needed to receive it. (https://cloud.ouraring.com/docs/authentication)
- **Access-token TTL: not a single documented number — use `expires_in`.**
  - The token response documents only: `expires_in` — "Number of seconds that the access token is valid
    for." (https://cloud.ouraring.com/docs/authentication)
  - The v2 docs' Authentication-Troubleshooting section says: "OAuth2 access tokens expire after a period
    (**typically 24 hours** for Oura)." (https://cloud.ouraring.com/v2/docs, tag "Authentication Troubleshooting")
  - The v2 docs' FAQ says the opposite: "The token hasn't expired (**they typically last 30 days**)."
    (https://cloud.ouraring.com/v2/docs, tag "Frequently Asked Questions")
  - The client-side (implicit) flow's token is "currently set to 30 days" and that flow "does not support
    refresh tokens." (https://cloud.ouraring.com/docs/authentication)
  - Verdict: docs are internally inconsistent; treat `expires_in` as the only truth.
- **Authorization code**: "valid for only 10 minutes" and single-use ("or already used" → `invalid_grant`).
  (https://cloud.ouraring.com/v2/docs, tag "Authentication Troubleshooting")
- **Client auth at `/oauth/token`**: `client_id`+`client_secret` as form params, or HTTP Basic. Params must be
  `application/x-www-form-urlencoded` UTF-8. (https://cloud.ouraring.com/docs/authentication)
- **PKCE**: not mentioned anywhere in the authentication docs — docs don't say whether `code_challenge` is
  supported. (The gateway is a confidential client, so this doesn't block the design.)

## 3. Dev app registration

- **Oura account required.** "To use the Oura API, you'll need an Oura account and an API application."
  (https://support.ouraring.com/hc/en-us/articles/4415266939155-The-Oura-API). Consistent with behavior:
  `https://cloud.ouraring.com/oauth/applications` 302s to
  `https://cloud.ouraring.com/user/sign-in?next=%2Foauth%2Fapplications` (verified empirically 2026-07-17).
- **Ring / active membership required to register an app?** **Docs don't say.** The support article requires
  a ring+membership to access *your own data*, but states no hardware/membership condition for creating an
  application. The sandbox exists precisely for development "without an Oura account" (see §6).
- **Two registration surfaces, in transition.** The support article points to "our newest developer portal"
  — `https://developer.ouraring.com` (sign-in gated; its public landing page links back to
  https://cloud.ouraring.com/v2/docs for documentation) — while the v2 docs' Quick Start still links the
  legacy `https://cloud.ouraring.com/oauth/applications`. Both are live today.
- **User cap and approval**: "API Applications are limited to **10** users before requiring approval from
  Oura. There is no limit once an application is approved." (https://cloud.ouraring.com/v2/docs, "Data
  Access" section). FAQ adds: "the Oura API is free to use for both personal and commercial applications.
  However, API applications accessing more than 10 users require approval from Oura."
  (https://cloud.ouraring.com/v2/docs, tag "Frequently Asked Questions"). **How approval works (form,
  criteria, review time) is not documented anywhere public**; the only documented contact channel is
  `api-support@ouraring.com` (given in the v2 docs for rate-limit raises).
- **Redirect-URI rules**:
  - "The redirect URIs listed for your application act as a whitelist of allowed values, meaning this
    parameter must match one of the URIs exactly." (https://cloud.ouraring.com/docs/authentication)
  - Multiple URIs per app are supported (implied by "If your application has been configured with exactly
    one redirect URI, this parameter can be left out", and by the plural "URIs" throughout; also error
    "No redirect URIs set in the application … at least one valid redirect_uri" —
    https://cloud.ouraring.com/docs/error-handling).
  - `redirect_uri` is *optional* on `/oauth/authorize` when exactly one URI is registered (docs still advise
    always sending it).
  - **https-only? localhost allowed? Docs don't say.** The registration form sits behind sign-in, and no
    public page states scheme/host restrictions. (The HTTPS requirement stated in the webhook docs applies
    to webhook callback URLs, not OAuth redirects.)

## 4. Endpoints, pagination, membership gating, read-only-ness

Base URL: `https://api.ouraring.com` (per every example in https://cloud.ouraring.com/v2/docs; note the
OpenAPI `servers` entry is literally the broken string `https://api.None.com` — a spec-generation bug,
don't consume it programmatically).

**Data routes** (all `GET`, all under `/v2/usercollection/`, from `openapi-1.35.json` → `paths`). Every
route also has a `GET …/{document_id}` single-document variant except `heartrate`, `personal_info`, and
`ring_battery_level`:

| route | notes |
|---|---|
| `daily_activity` | daily activity summary (MET minutes) |
| `daily_cardiovascular_age` | |
| `daily_readiness` | |
| `daily_resilience` | |
| `daily_sleep` | daily sleep score/summary |
| `daily_spo2` | "Data will only be available for users with a Gen 3 Oura Ring" (v2 docs, tag "Daily Spo2 Routes") |
| `daily_stress` | |
| `enhanced_tag` | |
| `heartrate` | 5-minute-increment time series (v2 docs, tag "Heart Rate Routes") |
| `personal_info` | see §5 |
| `rest_mode_period` | |
| `ring_battery_level` | list route only |
| `ring_configuration` | model/size/color; hardware enum includes `gen1…gen4, or5` |
| `session` | |
| `sleep` | detailed sleep periods ("A user can have multiple sleep periods per day") |
| `sleep_time` | bedtime-window recommendations |
| `tag` | **deprecated** — "Tag is deprecated. We recommend transitioning to Enhanced Tag" (v2 docs, tag "Tag Routes") |
| `vO2_max` | note the odd capitalization in the path |
| `workout` | |

**Pagination shape** (from the list-route parameters in `openapi-1.35.json`):
- Standard list routes: `start_date`, `end_date` (accept `date` or `date-time`, optional), `next_token`
  (string, optional), and `fields` (optional "Comma-separated list of fields to include in the response,
  in addition to the always returned fields. Defaults to all fields if not provided.").
- `heartrate` differs: `start_datetime`, `end_datetime` (date-time), `next_token`, `latest` (boolean, "If
  True, returns most recent sample."), `fields`.
- Response envelope: `{"data": [...], "next_token": <string|null>}` (`MultiDocumentResponse*` schemas;
  observed live on the sandbox). Feed a non-null `next_token` back as the query param to get the next page;
  `null` means done. **Page size is not documented.**
- Date params "are interpreted in the user's local timezone" (v2 docs, tag "Frequently Asked Questions").

**Membership gating**:
- "Gen3 and later users without active Oura Membership can't access their data through the Oura API."
  (https://support.ouraring.com/hc/en-us/articles/4415266939155-The-Oura-API)
- 403 semantics: "The requested resource requires additional permissions or the user's Oura subscription
  has expired." (https://cloud.ouraring.com/v2/docs, response-code table); troubleshooting: "The user needs
  to renew their Oura membership to restore API access." (tag "Authentication Troubleshooting")
- Hardware gating on top: `daily_spo2` is Gen 3+ only; the `heartrate` scope is described as "for Gen 3 users".

**Read-only?** Yes for data — every `usercollection` route is `GET`-only; there are no write endpoints for
user data anywhere in the spec. The only non-GET routes are **webhook subscription management**
(`POST /v2/webhook/subscription`, `PUT /v2/webhook/subscription/{id}`, `PUT /v2/webhook/subscription/renew/{id}`,
`DELETE /v2/webhook/subscription/{id}`), which authenticate with the app's `x-client-id`/`x-client-secret`
headers, not a user token (`openapi-1.35.json` → `paths`, `components.securitySchemes`).

## 5. Identity: `personal_info` and email

- `GET /v2/usercollection/personal_info` → `PersonalInfoResponse`: `id` (string, required), `age`,
  `weight`, `height`, `biological_sex`, **`email`** — all nullable except `id`
  (`openapi-1.35.json` → `components.schemas.PersonalInfoResponse`).
- **Email is exposed**, guarded by the `email` scope ("Email address of the user" —
  https://cloud.ouraring.com/docs/authentication).
- "You can access the **id** on the personal_info route with **any access token (no scopes are required)**."
  (https://cloud.ouraring.com/v2/docs, tag "Personal Info Routes")
- Gateway implication: keying accounts by normalized login email works **only if the user grants the
  `email` scope** — and Oura's consent screen lets users untick individual scopes (§1), so `email` can come
  back `null` even when requested. The always-present, scope-free identifier is `id`. Either enforce the
  `email` grant at connect time (fail the authorize flow if `personal_info.email` is null, mirroring how
  WHOOP verify works) or key on Oura's `id` instead.

## 6. Rate limits and sandbox

- **Numeric limit**: "The API V1 and V2 API are rate limited to **5000 requests in a 5 minute period**. You
  will receive a 429 error code if you exceed this limit." (https://cloud.ouraring.com/docs/error-handling;
  same figure in the v2 docs FAQ: "limited to 5000 requests per 5-minute period").
- **Newer two-layer description** (v2 docs overview, https://cloud.ouraring.com/v2/docs): "a
  **per-access-token** limit, which throttles single-token floods, and a **per-application** limit, which
  caps the aggregate traffic across all of an application's end-user tokens". A 429 carries five headers:
  `Retry-After` (seconds), `X-RateLimit-Limit`, `X-RateLimit-Window`, `X-RateLimit-Reset` (epoch s),
  `X-RateLimit-Tier` (which layer fired). No numeric ceilings are published for the two layers; the docs say
  to prefer the headers over fixed backoff and to "[Contact us](mailto:api-support@ouraring.com) if you
  expect your usage to require higher limits."
- **Webhooks are the pressure valve**: "Webhooks are the preferred way to consume Oura data … We have not
  had customers hit rate limits with webhooks properly implemented." (https://cloud.ouraring.com/v2/docs,
  overview + tag "Webhook Subscription Routes"; events arrive ~30 s after the user's app sync, HMAC-signed
  via `x-oura-signature`/`x-oura-timestamp` with the client secret).
- **Sandbox**: a parallel route exists for **every** data type at `GET /v2/sandbox/usercollection/<type>`:
  "Fake user data that you can access **without an Oura account**. … useful for testing and development
  purposes. … The rate limit for the sandbox endpoints is shared with your rate limit on other data
  endpoints." (https://cloud.ouraring.com/v2/docs, tag "Sandbox Routes")
  - Verified empirically 2026-07-17: with no `Authorization` header the sandbox returns
    `400 {"detail":"Missing auth token. Include any string in 'Authorization' header."}`; with
    `Authorization: Bearer dummy` it returns `200` with plausible synthetic documents and the standard
    `{"data": [...], "next_token": null}` envelope. So: any string works, no registration needed —
    development and CI can run against it without a ring, account, or app.

## Watch out (gateway-integrator flags)

- **PATs were removed in December 2025** — any older Oura integration guide showing PAT-based auth is
  obsolete; an OAuth application is mandatory. (https://cloud.ouraring.com/v2/docs)
- **Refresh rotation is exactly the WHOOP failure mode** this gateway already engineered around: single-use
  refresh tokens, reuse → "Token Already Used Or Revoked". Reuse Whoop's pattern: per-account lock,
  persist the rotated pair before first use. Difference from WHOOP: **no `offline`-scope analog** — nothing
  extra to request to keep getting refresh tokens.
- **Email-keying is not guaranteed**: the `email` scope is user-declinable at consent and the field is
  nullable — the connect flow must verify `personal_info.email` is present (or fall back to Oura's `id`)
  before minting an account key.
- **10-user cap before approval** — fine for the "small trusted circle" today, but the approval process is
  entirely undocumented (no form, criteria, or SLA published); budget lead time via
  api-support@ouraring.com if the circle grows.
- **Registration surface is mid-migration**: legacy `cloud.ouraring.com/oauth/applications` vs the "newest
  developer portal" `developer.ouraring.com`. Expect UI/docs churn; re-check redirect-URI constraints
  (https/localhost — currently undocumented) inside whichever portal the app gets created in.
- **Docs self-contradict on access-token TTL** (24 h vs 30 d) — implement strictly against `expires_in`.
- **Scope-string ambiguity** `spo2` vs `spo2Daily` between the auth page and the OpenAPI spec — test
  against the live authorize endpoint before shipping the scope list.
- **Membership gating**: a connected Gen3+ user whose membership lapses turns into 403s mid-life — map that
  to the gateway's stale-auth 502 shape with a "renew your Oura membership" hint rather than retrying.
- **Per-application aggregate rate limit**: all of the gateway's users share one app-level bucket — a
  single heavy user can starve the rest; the `X-RateLimit-Tier` header distinguishes token-level vs
  app-level throttling.
- **`tag` route is deprecated** in favor of `enhanced_tag` — don't expose the old one as a tool.
- Minor spec bugs to not trip on: OpenAPI `servers` says `https://api.None.com` (use
  `https://api.ouraring.com`), and `vO2_max` really is capitalized like that in the path.
- **V1 is gone** ("removed on January 22nd, 2024" per https://cloud.ouraring.com/docs); v2 is "the only
  available integration point" — no upcoming v3 is mentioned anywhere in the current docs.
