# 02 — Register the Oura dev application

Type: task
Status: open
Blocked by: 01

## Question

Get a real Oura API application registered under the operator's account, so its concrete
constraints (client id/secret, redirect URI acceptance, cap, scopes as actually granted)
are facts rather than documentation claims. HITL where the portal requires the human
(account creation, e-mail verification).

Record in the answer: where the credentials live (Railway env vars + `.env` skeleton),
the registered redirect URI (`https://missingmcp.com/oura/oauth/callback` expected),
and any surprises vs. ticket 01's research (e.g. approval queue, cap different than
documented, https/localhost redirect rules — undocumented per ticket 01). Note: a new
portal `developer.ouraring.com` is mid-rollout beside the legacy cloud.ouraring.com —
prefer whichever actually issues working credentials and record which one it was.

## Comments

**2026-07-17 — how to register without owning a ring** (operator has no ring; the
research found no documented ring/membership requirement — an Oura *account* is the
only prerequisite):

1. Try `developer.ouraring.com` first (redirects to signin with
   `callbackUrl=/applications`) — look for a sign-up path; the new portal is the most
   likely to take a plain email account.
2. If it demands an existing Oura account: create one at cloud.ouraring.com or in the
   Oura mobile app (recent versions allow an account without a paired ring).
3. If both dead-end at "pair your ring": email api-support@ouraring.com — registering
   as an integrator without hardware is a legitimate scenario, and the same channel
   handles the 10-user-cap approval later anyway.
4. Last resort (not recommended): register under the beta tester's account — keys and
   approval would hang on an account the operator doesn't control.

Implementation is NOT blocked by this ticket in practice: the sandbox
(`/v2/sandbox/usercollection/*`) works with no account at all; the app is needed only
for the real end-to-end OAuth smoke test.
