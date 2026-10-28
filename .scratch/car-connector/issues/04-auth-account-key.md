# 04 — BYO-key auth, account_key, expiry

Type: grilling
Status: resolved
Blocked by: 01, 02

## Question

How does a person connect: what does the paste-key form ask for (key only? key + VIN?
key + email?), how is the key verified before persist, what is the durable `account_key`
(email / upstream user id / VIN / key-derived), how are several cars per person handled, and
how does key expiry surface (warning before `X-API-Key-Expires-At`, re-auth 401 after)?

Constraints from 01: the API exposes **no user identity** and **no VIN listing** — the user
must supply VIN(s), so `account_key` cannot be the login email as for garmin/whoop. Whether
to collect and store the **S-PIN** (required by aux heating) is decided here as a credential
question; 05 decides whether aux heating ships at all.

## Answer

Decided with Vaclav, 2026-10-28.

- **`account_key` = the email the person types on the form**, normalized via
  `base.normalize_account_key`. It is **unverified**. Why: the whole system assumes
  `account_key` is an email: campaigns select `account_key AS email FROM accounts`, PostHog
  uses it as `distinct_id`, and `normalize_account_key` is contracted as "login email". A
  VIN-based key would mail VINs. Risk accepted: someone could type a foreign address. Unsubscribe
  is global. Verification (confirm link) may come later (fog).
- **Form (`/car/oauth/authorize`)**: brand (only Škoda for now), email, API key, VIN. Setup copy:
  "MyŠkoda app → Profil → Chytrá domácnost → Vytvořit klíč; copy the VIN on the same screen",
  ideally with a screenshot, plus a line that the email is used only for important connector
  news. **v1: one VIN per account.**
- **Verify-then-persist**: `verify` = one `GET /api/v1/vehicles/{vin}?include=info,operations` (amended by 05) via
  `_bounded`. On 200, store `X-API-Key-Expires-At` in the blob. 401 means invalid key, 403
  (`api-key-not-authorized`) means the key doesn't cover this VIN, 429 means "budget used up, try
  in N min" (from `Retry-After`). Each error re-renders the form. Costs 1 of the 20/h.
- **Blob** (encrypted): brand `skoda`, API key, VIN, `expires_at`. Never logged.
- **Expiry (fixed ~6 months)**: an upstream 401 (`api-key-expired` or `about:blank`) maps to the
  gateway re-auth 401 via `_reauth_required`; the person pastes a fresh key. **From 14 days
  before `expires_at`**, tool results carry a short notice: "your Škoda key expires on <date>,
  create a new one in MyŠkoda". Email reminders come later (fog).
- **S-PIN is never collected or stored.** S-PIN-requiring actions (auxiliary heating start) are
  **not in v1**. It's the account's security PIN (it also unlocks the car in the app), and the
  risk outweighs the benefit for a feature only some ICE cars have.
- Glossary: the `CONTEXT.md` **Account** entry now covers the typed, unverified email.
