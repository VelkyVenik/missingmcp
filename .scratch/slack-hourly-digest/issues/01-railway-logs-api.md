# 01 — Railway logs API: fetch the last hour unattended

Type: research
Status: resolved

## Question

How does an unattended job (GitHub Actions, no `railway` CLI login) fetch a
service's recent logs from the Railway **GraphQL API**? Produce a markdown asset
with a working query and the auth/limits facts. Specifically:

1. **Auth**: which token works from CI — an account token (`RAILWAY_API_TOKEN`) or a
   project token (`RAILWAY_TOKEN`)? Which header (`Authorization: Bearer` vs
   `Project-Access-Token`)? Endpoint (`https://backboard.railway.com/graphql/v2`?).
2. **Query**: the exact GraphQL query the CLI uses for runtime logs
   (`deploymentLogs` / `environmentLogs`?), its required variables
   (deploymentId / environmentId / serviceId), and the returned JSON shape (does it
   carry the structured message fields, or just a text line we'd re-parse?).
3. **Time window**: can we request "the last 60 minutes" (a `startDate`/`filter`
   arg), or only "last N lines"? If only N-lines, what's the max, and how do we
   bound an hour reliably?
4. **Which id to target**: the active deployment id changes on every deploy — how
   does a static CI job resolve the *current* deployment/environment id at run time
   (a query for the latest active deployment of a service)?
5. Rate limits / auth-scope gotchas for CI use.

Reference: the `use-railway` skill + `scripts/railway-api.sh` in this repo (the CLI
already does all of this — reverse-engineer the query it issues). Write findings to
`.scratch/slack-hourly-digest/assets/railway-logs-api.md`, cite Railway docs / the
CLI source where possible.

## Answer

Resolved 2026-07-18. Full findings + query snippets: [assets/railway-logs-api.md](../assets/railway-logs-api.md).

- **Auth:** account/workspace token `RAILWAY_API_TOKEN` with `Authorization: Bearer <token>`
  at `https://backboard.railway.com/graphql/v2`. Project token (`Project-Access-Token`
  header) is NOT confirmed to read logs — don't rely on it.
- **Query:** `deploymentLogs(deploymentId!, startDate, endDate, filter, limit): [Log!]!`;
  `Log = { timestamp, message, severity, attributes{key,value}, tags{} }`. Only
  `deploymentId` is required.
- **Time window: a TRUE one exists** — `startDate`/`endDate` are RFC3339 DateTimes; set
  `startDate = now−60min`. Pass a generous `limit` (~5000) as a safety ceiling. No
  N-lines-and-reslice hack needed.
- **Resolve current deployment id at runtime:** `serviceInstance(environmentId, serviceId)
  { latestDeployment { id status } }` (one call), or `deployments(input:{serviceId,
  environmentId}, first:N)` → first `SUCCESS`.
- **Structured fields recoverable:** Railway maps JSON stdout `level`→`severity` and other
  keys→`attributes[]`.
- **Limits:** generous (100/hr Free … 10000/hr Pro; our 2-req/hr job is trivial). Log
  retention 7d Hobby / 30d Pro. `timestamp` is an RFC3339 string.

**⚠️ One-time validation for the build (ticket 04):** our `log.py` emits no top-level
`message` key, and Railway treats `message` as the canonical structured-log field. It's
unconfirmed whether Railway still promotes our other keys into `attributes[]` when
`message` is absent, or dumps the raw JSON into `message`. Validate once against prod with
a real token; **keep a "JSON-parse `message` yourself" fallback** in the summarizer.
