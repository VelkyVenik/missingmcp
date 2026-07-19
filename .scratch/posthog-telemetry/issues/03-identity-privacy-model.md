# 03 — Identity & privacy model

Type: grilling
Status: open
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
