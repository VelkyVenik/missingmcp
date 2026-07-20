# 03 — Identity & privacy model

Type: grilling
Status: resolved
Blocked by: 01

## Question

What identity do events carry to PostHog, and what data is allowed to leave our box?

1. **distinct_id for server events**: full `account_key` (a lowercased email — PII at a
   third party) vs. the 8-char hash prefix already used in logs vs. a dedicated stable
   hash. CLAUDE.md's logging invariant (8-char hash prefix max) was written for Railway
   logs — decide whether PostHog gets the same treatment or a deliberate, recorded
   exception.
2. **Web ↔ account stitching**: campaign analytics wants anonymous visitor → connected
   account joined up. Where/how does identify/alias happen (the OAuth success page?), and
   is that acceptable privacy-wise for the trusted circle?
3. **Property allowlist**: which event properties may leave (tool names, latencies,
   adapter — presumably yes; emails, tokens, IPs — ?), stated as a rule the spec locks
   down.

## Answer

Decided with the operator, 2026-07-20:

1. **distinct_id = the plain normalized email** (no adapter scoping): person = human,
   `adapter` is an event property — the research asset's `SHA-256(adapter:email)`
   suggestion would fragment one human into one person per adapter, rejected. Grounding
   fact: the gateway already logs the full `account_key` to Railway (`proxy.py`,
   `mcp-response` … `account=key`) — the "8-char hash prefix" invariant covers *secrets*,
   not account identity. The spec records: **PostHog is the same trust class as Railway
   logs**, and **person deletion (API) joins the account-revoke path**.
2. **Web ↔ account stitching: server-side via the posthog cookie.** posthog-js runs on
   the landing AND the OAuth form pages (same domain ⇒ same anonymous cookie); on
   successful authorize (form login or upstream-OAuth callback) the gateway reads the
   anonymous id from the cookie and sends `$identify` (`distinct_id`=email,
   `$anon_distinct_id`=cookie id). No UX change, no interstitial. Accepted loss:
   cross-device journeys (landing on desktop, connect on mobile) don't stitch.
3. **Egress rule: "identity and metadata yes, content never."** Allowed: email as
   distinct_id only, adapter, tool name, status, latency/bytes, UTM, page paths,
   user-agent. Never: MCP request/response bodies (health data!), passwords/tokens/codes,
   form contents. Raw IP discarded project-wide (GeoIP country/city kept). Autocapture
   only on marketing pages; OAuth/sign-in pages get explicit pageviews only.
