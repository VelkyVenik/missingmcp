# 01 — Sharing one Claude account with family: document the `?u=` connector trick

Type: task
Status: open (todo — do first)
Blocked by: —

## Context

A user shares one Claude Pro account with his daughter; each has their own
Garmin account. Claude doesn't allow two connectors with the same URL.

Verified workaround (user-tested 2026-10-19): add the second connector with a
dummy query param, e.g.

    https://missingmcp.com/garmin/mcp?u=daughter

Claude accepted both connectors, each did its own Garmin sign-in. Isolation
OK: correct profile and correct same-day runs per connector, no crossover.
Matches the code: no resource check, separate DCR per connector, accounts
keyed by the Garmin login. We already replied to the user and promised to put
it on the web.

## Task

Add the trick to the website so people can find it — a short section like
**"Sharing one Claude account with family"** on `/garmin` (e.g. in
`#connect` or `#tips` of `src/missingmcp/templates/garmin.html`; there is no
FAQ section today). Content:

- Claude allows one connector per URL → add each extra person's connector
  with any unique `?u=<name>` suffix.
- Each connector signs in to its own Garmin account; data never mixes.
- Name the connectors so it's clear whose is whose (e.g. "Garmin — Dad", "Garmin — Daughter").

Outward-facing copy shown to the operator before merge. Consider the same
note on `/garmin/chatgpt` if ChatGPT has the same one-URL limit (verify).
