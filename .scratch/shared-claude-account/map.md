# Wayfinder map: One Claude account, several Garmin accounts

Label: wayfinder:map
Created: 2026-10-20

## Destination

People who share one Claude account (typically a family) can find, on the
website, how to connect **each person's own Garmin account** as a separate
connector, and the setup is unambiguous: the documented `?u=` trick first, then
(later) proper per-user connector URLs whose OAuth protected-resource metadata
matches the URL the client was given (RFC 9728 strict).

## Notes

- **Origin (2026-10-19):** a user shares one Claude Pro account with his
  daughter; each has their own Garmin account. Claude refuses a second
  connector with the same URL. Verified workaround (user-tested 2026-10-19):
  add the second connector with a dummy query param, e.g.
  `https://missingmcp.com/garmin/mcp?u=daughter`. Claude accepted both, each
  ran its own Garmin sign-in; isolation OK (correct profile and same-day runs
  per connector, no crossover). We replied and promised to put it on the web.
- **Why it works (code):** no `resource` check against the query string,
  each connector registers its own OAuth client (separate DCR), accounts are
  keyed by the Garmin login — so two connectors = two independent accounts.
- **Every code change via feature branch + PR**; ask the operator first; show
  outward-facing text first. See [[branch-pr-workflow]], [[approve-public-posts]].
- **Public repo**: no user names or e-mails in any ticket — the origin user
  stays anonymous here.

## Decisions so far

<!-- one line per closed ticket: gist + link -->

## Tickets

- [01 — Document the `?u=` trick on the website](issues/01-document-shared-claude-account-trick.md)
  — todo, do first.
- [02 — Per-user connector paths with matching protected-resource metadata](issues/02-per-user-connector-paths.md)
  — todo, later.

## Out of scope

- Multiple Garmin accounts inside **one** connector (account switching) —
  not asked for; separate connectors keep isolation trivial.
