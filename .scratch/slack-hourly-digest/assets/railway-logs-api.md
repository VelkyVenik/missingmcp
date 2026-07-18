# Railway logs via the GraphQL API (headless CI)

_Researched 2026-07-18. For the hourly Slack digest (ticket 01). No `railway` CLI login available in CI._

Primary sources: Railway public-API reference (<https://docs.railway.com/reference/public-api>),
Railway logs/observability docs (<https://docs.railway.com/observability/logs>),
the Railway CLI source (<https://github.com/railwayapp/cli> — it issues exactly these queries),
and the CLI's committed GraphQL schema
(<https://raw.githubusercontent.com/railwayapp/cli/master/src/gql/schema.json>).

---

## TL;DR (the 5 questions, one line each)

1. **Auth**: Use an **account or workspace token** (`RAILWAY_API_TOKEN`) with header
   `Authorization: Bearer <token>` against `https://backboard.railway.com/graphql/v2`.
   A project token (`RAILWAY_TOKEN`) uses `Project-Access-Token: <token>` but its ability to
   read `deploymentLogs` is **not documented — don't rely on it**. Bearer account/workspace token is the safe choice.
2. **Query**: `deploymentLogs(deploymentId, startDate, endDate, filter, limit)` → `[Log!]!` where each
   `Log { timestamp, message, severity, attributes { key value }, tags { … } }`. **Structured fields ARE
   recoverable** — Railway parses JSON stdout into `attributes[]` (key/value) + `severity`; caveat below.
3. **Time window**: **Yes — a true time window exists.** `deploymentLogs` takes `startDate`/`endDate`
   (`DateTime`, RFC3339). Set `startDate = now − 60min`. No documented `limit` cap; pass `limit` as a
   secondary safety ceiling. (`environmentLogs` instead uses `afterDate`/`beforeDate`/`anchorDate` cursors.)
4. **Current deployment id**: resolve at runtime with
   `serviceInstance(environmentId, serviceId){ latestDeployment { id status } }` (single call), or the
   CLI's approach — `deployments(input:{serviceId,environmentId}, first:N)` then take the first `SUCCESS`.
5. **Gotchas**: rate limits are generous for hourly (100 req/hr Free … 1000 Hobby … 10000 Pro); logs are
   **retained 7d Hobby / 30d Pro**; the biggest risk is the `message`-vs-`attributes` mapping for our
   no-`message`-key JSON events (validate against prod once).

**Recommendation:** `RAILWAY_API_TOKEN` (account or, better for a team, workspace token) via
`Authorization: Bearer`, calling `deploymentLogs` with `startDate`/`endDate`. **A real time-window query
exists — no need to over-fetch N lines and re-slice.**

---

## 1. Auth for CI

Endpoint (confirmed by both the public-API docs and this repo's `use-railway` helper
`scripts/railway-api.sh`):

```
POST https://backboard.railway.com/graphql/v2
Content-Type: application/json
```

Token types (source: <https://docs.railway.com/reference/public-api>):

| Token | Scope | Header | Doc use-case |
|---|---|---|---|
| **Account** (`RAILWAY_API_TOKEN`) | All your resources & workspaces | `Authorization: Bearer <token>` | Personal scripts, local dev |
| **Workspace/Team** (`RAILWAY_API_TOKEN`) | A single workspace | `Authorization: Bearer <token>` | **Team CI/CD, shared automation** |
| **Project** (`RAILWAY_TOKEN`) | A single environment in a project | `Project-Access-Token: <token>` | Deployments, service automation |

Key facts:
- The account and workspace tokens **both** use `Authorization: Bearer`. The project token is the odd one
  out: it uses the `Project-Access-Token` header, **not** `Authorization: Bearer` (docs call this out
  explicitly).
- **Which token reads logs?** The docs do **not** state per-token permissions for `deploymentLogs`. The
  account token's scope is "all your resources" so it can. The CLI itself authenticates with the account
  token it stores in `~/.railway/config.json` (`.user.token`) and sends `Authorization: Bearer` — i.e. the
  CLI reads logs with an account/Bearer token (see `scripts/railway-api.sh` in this repo, which mirrors
  that exactly). **Project-token log access is unconfirmed by primary sources — treat as untested.**

**For this CI job:** generate an **account token** (or, if the project lives in a Railway team/workspace,
a **workspace token** — narrower blast radius) at
<https://railway.com/account/tokens>, store it as the GH secret `RAILWAY_API_TOKEN`, and send
`Authorization: Bearer $RAILWAY_API_TOKEN`.

```bash
curl -s https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"query{ me { name email } }"}'   # smoke test the token
```

---

## 2. The logs query

Reverse-engineered from the CLI's committed query file
`src/gql/queries/strings/DeploymentLogs.graphql`
(<https://github.com/railwayapp/cli/blob/master/src/gql/queries/strings/DeploymentLogs.graphql>) and
confirmed against `schema.json`:

```graphql
query DeploymentLogs(
  $deploymentId: String!
  $limit: Int
  $filter: String
  $startDate: DateTime
  $endDate: DateTime
) {
  deploymentLogs(
    deploymentId: $deploymentId
    limit: $limit
    filter: $filter
    startDate: $startDate
    endDate: $endDate
  ) {
    timestamp        # RFC3339 (nano), String
    message          # the log message content, String!
    severity         # e.g. "info"/"err" (String, nullable) — Railway maps JSON `level` → severity
    attributes {     # parsed structured fields — [LogAttribute!]!
      key
      value
    }
    tags {           # LogTags: deploymentId, deploymentInstanceId, environmentId,
      serviceId      #          pluginId, projectId, serviceId, snapshotId
      deploymentId
    }
  }
}
```

Schema signature (from `schema.json`):
`deploymentLogs(deploymentId: String!, endDate: DateTime, filter: String, limit: Int, startDate: DateTime): [Log!]!`
— "Fetch logs for a deployment."

**Required variable:** only `deploymentId` (a String, the deployment UUID). `serviceId`/`environmentId`
are **not** taken by `deploymentLogs` — you resolve them into a `deploymentId` first (§4).

`Log` type (from `schema.json`):

| Field | Type | Meaning |
|---|---|---|
| `message` | `String!` | The log message content |
| `timestamp` | `String!` | RFC3339 (nano) |
| `severity` | `String` | Severity, e.g. `err` (Railway sets this from a JSON `level`) |
| `attributes` | `[LogAttribute!]!` | Fields parsed from a structured log — `{ key, value }` |
| `tags` | `LogTags` | `{ projectId, environmentId, serviceId, deploymentId, deploymentInstanceId, pluginId, snapshotId }` |

`LogAttribute` is `{ key: String!, value: String! }`.

### Are our structured fields recoverable? Yes (with one caveat)

Railway normalizes stdout logs: per the logs docs, a JSON line's `msg`/`message` → `message`, `level` →
`severity`, and **every other key becomes a queryable attribute** (`@key:value` filter syntax; array
elements via `@arr[i]:value`). So the gateway's structured JSON
(`{"ts":…,"level":"info","event":"mcp-response","account":…,"status":"200","tool":…,"ttfb_ms":…}`,
emitted by `src/missingmcp/log.py::_emit`) surfaces as:
- `severity` ← our `level` (`info`/`warn`/`error`/…),
- `attributes[]` ← `event`, `account`, `status`, `tool`, `ttfb_ms`, `total_ms`, `bytes`, `ts`, `reason`, …

So the digest reads the structured signal from `severity` + `attributes[]` — it does **not** have to
re-parse a flat text line. Filter server-side too, e.g. `filter: "@level:error"` or
`filter: "@event:mcp-response"` (same filter grammar as `railway logs --filter`,
<https://docs.railway.com/cli/logs>).

> **Caveat (see Watch out):** our own events do **not** include a top-level `message` key — `log.py`
> writes `ts`/`level`/`event`/custom fields only (bridged stdlib/uvicorn records are the exception; those
> do carry `message`). Railway docs call `message` the "required" field of a structured log, so it's
> unconfirmed from docs alone whether Railway (a) still promotes our other keys into `attributes[]` when
> `message` is absent, or (b) dumps the whole raw JSON line into `message` with an empty `attributes[]`.
> Validate against production before trusting `attributes[]` exclusively; keep a "JSON-parse `message`
> yourself" fallback.

---

## 3. Time window — a real one exists

**Yes.** `deploymentLogs` accepts `startDate` and `endDate` (`DateTime`, RFC3339). This is a genuine
time-window filter, not just "last N lines" — the CLI's `--since`/`--until` map straight onto them
(CLI `logs.rs`: `start_date = --since`, `end_date = --until`, passed in `FetchLogsParams { deployment_id,
limit, filter, start_date, end_date }`;
<https://github.com/railwayapp/cli/blob/master/src/commands/logs.rs>).

For the hourly digest, the clean call is:

```json
{ "deploymentId": "<id>", "startDate": "2026-07-18T12:00:00Z", "endDate": "2026-07-18T13:00:00Z", "limit": 5000 }
```

Notes:
- `limit` is an optional `Int`. **No maximum is documented** in the public docs or schema. The CLI leaves
  it unset when only a time window is requested (`--lines` is `Option<i64>`, no default;
  `railway logs` "fetch[es] historical logs if --lines, --since, or --until … are provided").
- Because it's undocumented whether an **omitted** `limit` returns the full window unbounded or hits a
  server default cap, pass an explicit generous `limit` (e.g. 5000) as a ceiling alongside `startDate`.
  The gateway is low-volume, so a 60-minute window will sit far under that; if a run ever hits the ceiling,
  that itself is an anomaly worth flagging.
- Railway rate-limits ingestion at **500 log lines/sec/replica** (docs), so an hour's worth for this
  single-node gateway is comfortably bounded.
- Stateless by design: each run computes `startDate = now − 60min`, `endDate = now`. No cross-run cursor
  file needed (matches the map's "stateless windows" decision).

`environmentLogs` is the alternative but a worse fit here: its signature is
`environmentLogs(environmentId: String!, filter, afterDate, beforeDate, anchorDate, afterLimit,
beforeLimit): [Log!]!` — cursor-style `afterDate`/`beforeDate` (Strings) rather than a clean
`startDate`/`endDate`, it's environment-wide (you'd narrow with a `@service:` filter), and it excludes
build logs. Prefer `deploymentLogs`.

---

## 4. Resolving the current (active) deployment id at run time

The active deployment id changes on every deploy, so CI must discover it each run from a stable
`serviceId` + `environmentId`.

**Simplest — one call** (CLI query `LatestDeployment.graphql`,
<https://github.com/railwayapp/cli/blob/master/src/gql/queries/strings/LatestDeployment.graphql>):

```graphql
query LatestDeployment($serviceId: String!, $environmentId: String!) {
  serviceInstance(environmentId: $environmentId, serviceId: $serviceId) {
    latestDeployment { id status }
  }
}
```

`serviceInstance(environmentId: String!, serviceId: String!): ServiceInstance!` and
`ServiceInstance.latestDeployment: Deployment` are both confirmed in `schema.json`.

**CLI's exact approach — pick the newest `SUCCESS`** (query `Deployments.graphql`; the CLI defaults the
deployment to "most recent successful deployment, or latest deployment if none succeeded"):

```graphql
query Deployments($input: DeploymentListInput!, $first: Int) {
  deployments(input: $input, first: $first) {
    edges { node { id createdAt status meta } }
  }
}
```

with variables `{"input":{"serviceId":"<sid>","environmentId":"<eid>","projectId":"<pid>"},"first":10}`,
then take the first edge whose `status == "SUCCESS"`. `DeploymentListInput` fields (schema):
`projectId`, `environmentId`, `serviceId`, `status`, `includeDeleted`. `DeploymentStatus` enum values:
`BUILDING, CRASHED, DEPLOYING, FAILED, INITIALIZING, NEEDS_APPROVAL, QUEUED, REMOVED, REMOVING, SKIPPED,
SLEEPING, SUCCESS, WAITING`.

Use `LatestDeployment` for the digest (one round-trip); fall back to `Deployments` + `SUCCESS` filter only
if `latestDeployment` is ever in a non-running state you want to skip.

Finding the static ids once (store as GH secrets/vars): `railway status --json` gives environment id;
`railway service list --json` gives service id; or query
`project(id:"<pid>"){ services{ edges{ node{ id name } } } environments{ edges{ node{ id name } } } }`.

---

## 5. End-to-end example (serviceId + environmentId + token → last hour of logs)

Two requests. Compute the window in the shell, resolve the deployment, then fetch.

```bash
# Inputs (GH secrets / repo vars)
: "${RAILWAY_API_TOKEN:?}"; SERVICE_ID="<service-uuid>"; ENV_ID="<environment-uuid>"
API="https://backboard.railway.com/graphql/v2"
AUTH=(-H "Authorization: Bearer $RAILWAY_API_TOKEN" -H "Content-Type: application/json")

# Step 1 — resolve the current deployment id
DEPLOY_ID=$(curl -s "$API" "${AUTH[@]}" -d "$(jq -n \
  --arg sid "$SERVICE_ID" --arg eid "$ENV_ID" '{
    query: "query($sid:String!,$eid:String!){ serviceInstance(serviceId:$sid,environmentId:$eid){ latestDeployment { id status } } }",
    variables: { sid:$sid, eid:$eid }
  }')" | jq -r '.data.serviceInstance.latestDeployment.id')

# Step 2 — 60-minute window (GNU date; use `gdate` on macOS)
START=$(date -u -d '60 minutes ago' +%Y-%m-%dT%H:%M:%SZ)   # or: date -u -v-60M ...
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Step 3 — fetch the deployment logs for that window
curl -s "$API" "${AUTH[@]}" -d "$(jq -n \
  --arg did "$DEPLOY_ID" --arg s "$START" --arg e "$END" '{
    query: "query($did:String!,$s:DateTime,$e:DateTime,$lim:Int){ deploymentLogs(deploymentId:$did,startDate:$s,endDate:$e,limit:$lim){ timestamp severity message attributes{key value} } }",
    variables: { did:$did, s:$s, e:$e, lim:5000 }
  }')" | jq '.data.deploymentLogs'
```

Returned shape (per entry):

```json
{
  "timestamp": "2026-07-18T12:34:56.789012Z",
  "severity": "info",
  "message": "…",
  "attributes": [
    {"key": "event",  "value": "mcp-response"},
    {"key": "account","value": "…"},
    {"key": "status", "value": "200"},
    {"key": "tool",   "value": "get_activities"},
    {"key": "ttfb_ms","value": "123"}
  ]
}
```

The summarizer turns `attributes[]` into a dict per line, then buckets on `event`/`severity`/`status`
exactly like `tmp/logsum.py`.

Optional server-side prefilter to shrink payloads:
`"filter": "@level:error OR @level:warn"` or `"filter": "@event:mcp-response"`
(grammar: <https://docs.railway.com/cli/logs>).

---

## Watch out

- **`message` vs `attributes` for our events (highest priority).** `log.py` emits no top-level `message`
  key on the gateway's own events (only `ts`/`level`/`event`/custom fields; bridged stdlib/uvicorn records
  are the exception). Railway docs treat `message` as the required field of a structured log, so it is
  **not confirmed from docs** whether Railway still promotes our other keys into `attributes[]` when
  `message` is missing. **Action:** run the Step-3 query against production once with a real token and
  inspect a real `mcp-response` entry — confirm `attributes[]` is populated and `severity` carries our
  `level`. If not, JSON-parse the raw `message` line in the summarizer as a fallback. This is the one fact
  I could not close from primary sources alone.
- **Project token (`RAILWAY_TOKEN`) is not a confirmed path for logs.** It uses the `Project-Access-Token`
  header and the docs don't state it can read `deploymentLogs`. Don't build CI on it without testing; use
  a Bearer account/workspace token.
- **No documented `limit` cap.** Behavior of an omitted `limit` (full window vs server default) is
  unspecified — always pass an explicit ceiling with the time window.
- **Log retention** (docs): Hobby/Trial 7 days, Pro 30 days, Enterprise up to 90. Irrelevant for a
  60-minute window but note it if the plan is Trial and the digest ever backfills.
- **Rate limits** (public-API docs): tiered per plan — 100 req/hr (Free), 1000 (Hobby), 10000 (Pro);
  per-second 10 (Hobby)/50 (Pro). An hourly job doing 2 requests is nowhere near any tier. Response
  headers `X-RateLimit-Limit`/`-Remaining`/`-Reset` and `Retry-After` are returned; log them if you want
  guardrails. (These numbers are as summarized by the docs page on 2026-07-18; re-check if Railway
  revises tiers.)
- **`serviceInstance` requires the right `environmentId`.** A wrong/stale env id returns a different (or
  empty) `latestDeployment`. Pin `SERVICE_ID`/`ENV_ID` as repo vars and don't rely on any linked context.
- **`timestamp` is a String (RFC3339 nano), not an epoch.** Parse accordingly when ordering/bucketing.
- **Schema is a moving target.** Facts above are from the CLI's committed `schema.json` on 2026-07-18
  (`master`). If a query 400s, re-introspect the live endpoint with your token.

## Sources
- Public API auth, endpoint, rate limits: <https://docs.railway.com/reference/public-api>
- Structured-log parsing, filter grammar, retention: <https://docs.railway.com/observability/logs>, <https://docs.railway.com/cli/logs>
- Exact queries (reverse-engineered from the CLI): `DeploymentLogs.graphql`, `Deployments.graphql`, `LatestDeployment.graphql` under <https://github.com/railwayapp/cli/tree/master/src/gql/queries/strings> and the deployment-resolution logic in <https://github.com/railwayapp/cli/blob/master/src/commands/logs.rs>
- Schema signatures (`deploymentLogs`, `environmentLogs`, `Log`, `LogAttribute`, `LogTags`, `DeploymentListInput`, `ServiceInstance`, `DeploymentStatus`): <https://raw.githubusercontent.com/railwayapp/cli/master/src/gql/schema.json>
- In-repo prior art for the Bearer-token GraphQL call pattern: `use-railway` skill `scripts/railway-api.sh` + `references/request.md`
