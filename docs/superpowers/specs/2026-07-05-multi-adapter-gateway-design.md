# Multi-Adapter MCP Gateway — Design

**Date:** 2026-07-05
**Status:** Approved design (pre-implementation) — decisions resolved in a grill
session; supersedes nothing, *extends* `2026-06-30-garmin-mcp-gateway-design.md`
(which remains the reference for the Garmin-specific mechanics).

## Problem

Two working projects implement the same generic core twice, in two stacks:

| | `garmin-mcp-gateway` (this repo) | `rohlik-oauth-proxy` |
|---|---|---|
| Stack | Python 3.12 / Starlette / uv | Bun / TypeScript, zero deps |
| Size | ~1 400 lines | ~1 100 lines |
| OAuth 2.1 (DCR, PKCE S256, /token) | ✓ | ✓ (parallel implementation) |
| AES-256-GCM store in SQLite | ✓ | ✓ (parallel implementation) |
| Token hashing, rate limiter, security headers, structured logs | ✓ | ✓ (parallel implementation) |
| Upstream mechanics | interactive Garmin login (MFA) → per-user spawned `garmin-mcp` HTTP worker → reverse proxy | header injection (`rhl-email`/`rhl-pass`) → forward to hosted `mcp.rohlik.cz/mcp` |

Roughly 70 % of each codebase is the same generic layer; only the upstream part
differs — and it differs *in kind*, not in detail. Every security fix currently
has to be applied twice, in two languages. A third service would mean a third
copy.

Separately, both deployments live on a self-managed VPS (docker compose +
nginx). The operator wants to stop maintaining that box and get git-push
deploys (Railway).

## Decisions (from the grill session, 2026-07-05)

| Decision | Choice | Rationale |
|---|---|---|
| Audience | Personal infra for a small trusted circle | Unchanged from both parents. Not OSS-for-others, not SaaS — no stable public adapter API, no multi-tenant hardening. |
| Shape | **One gateway process, N adapters, path-based connectors** (`/garmin/mcp`, `/rohlik/mcp`) | One deploy, one DB, one OAuth implementation. In Claude, each upstream stays a *separate* connector with its own OAuth flow and tool list — the consolidation is invisible to users. |
| Core language | **Python**, evolved **in-place from this repo** | Garmin login runs `garminconnect` in-process and the worker is a Python CLI — a TS core would need Python IPC at the most fragile point (MFA flow). This repo has the tests, specs, and ops scripts worth keeping. Rohlik's upstream-specific logic is ~200 lines — the cheap side to rewrite. |
| Adapter scope | **Exactly two forward strategies:** (A) remote MCP + header injection, (B) per-account spawned HTTP worker | Both existing consumers are covered. No speculative modes (stdio↔HTTP bridge, native tools) — a third service will reveal its own shape when it arrives. |
| Storage | **Generic `accounts(adapter, account_key, blob_enc)`** table; `adapter` column added to `access_tokens`, `oauth_codes`, `oauth_clients`, `tool_usage` | The credential blob's shape is adapter-defined JSON (Garmin: tokens; Rohlik: email+password). Schema never changes again for a new adapter. One migration from `garmin_accounts`. |
| Hosting | **Railway**, single service + volume; **staging-first** on a temporary URL, custom domain flipped only after the spike test passes | Motivation: drop VPS maintenance, git-push deploys during the rebuild. Risk gate: Garmin login through Railway egress IPs (Cloudflare) must be proven on staging first. VPS keeps serving production until the flip. |
| URL continuity | **Clean paths, no legacy `/mcp` alias.** Garmin moves to `/garmin/mcp`. | Deliberate accept: the whole circle re-onboards once (re-add connector + Garmin login/MFA). Re-onboarding is staged over days because Garmin's Cloudflare limits are per-account. |
| Rohlik users | Re-login on the new gateway; **no `proxy.db` migration** | Rohlik login is email+password without MFA — a one-minute re-onboard. A one-off TS→Python re-encryption script costs more than it saves. |
| End state | Repo renamed `missingmcp`; `rohlik-oauth-proxy` archived | Rename is the *last* step, after the dust settles. |
| Name | **MissingMCP** — `missingmcp.dev` / `missingmcp.com`, GitHub `missingmcp` | See *Naming* below. |

### Why not the alternatives

- **Shared library + N services:** N deploys, N volumes, N Railway bills, and
  library versioning ceremony — for an audience of one operator. Rejected.
- **Template/copy-paste:** keeps the double-maintenance problem that motivated
  this work. Rejected.
- **One connector exposing all upstreams' tools under one Bearer token:** the
  authorize form would have to handle multiple services at once, per-service
  enable/disable in Claude's UI is lost, and Garmin alone has ~150 tools —
  one giant connector wastes context. Rejected in favor of path-based
  per-upstream connectors.
- **TS/Bun core:** would shell out to Python for the Garmin MFA flow. Rejected.

## Naming

**MissingMCP** (`missingmcp.dev`, `missingmcp.com`, GitHub `missingmcp`).
Chosen 2026-07-05 after weighing the "simple" family (`simplemcp.dev` etc.):

- Names the user's moment of need — "the connector is missing from Claude" —
  not the mechanism (gateway/shim/proxy require understanding the internals).
- Sets the right category expectation: "Simple/Easy/Fast + MCP" reads as an
  SDK for *writing* servers (FastMCP, EasyMCP); "mcpify" promises turning
  anything into MCP (explicitly out of scope). MissingMCP promises exactly
  what it does: the missing connectors, delivered.
- Established dev-culture lineage: Homebrew ("The Missing Package Manager"),
  MIT's Missing Semester, the Missing Manual series — community-made,
  pragmatic, gap-filling.
- Mechanism names are all squatted (mcpgate, mcpbridge, mcpforge, simple*.com);
  the problem-word identity was free across .com + .dev + GitHub simultaneously.
- Survives scope evolution: any future adapter is still "a connector missing
  from Claude"; if an official connector appears, that one simply stops being
  missing.

Tagline direction: *"The MCP servers exist. Connecting them shouldn't be
complicated."*

## Goals / Non-goals

**Goals**
- One codebase serving N upstreams as independent Claude connectors on one domain.
- Adding upstream N+1 = writing one adapter module (form fields + verify +
  forward strategy) + tests. No schema change, no new OAuth code.
- All security invariants of the Garmin gateway hold gateway-wide, including
  **verify-then-persist** — which the Rohlik TS proxy today *lacks* (it only
  format-validates credentials; wrong ones surface on first tool call). The
  Rohlik adapter gains a real verify step.
- Existing test suite survives the refactor and runs green at every step.
- Railway deployment with git-push deploys; SQLite on a volume; single replica.

**Non-goals**
- stdio↔HTTP bridging for arbitrary community MCP servers (mode B stays
  "HTTP-transport worker CLI", exactly what `garmin-mcp` provides).
- Writing native MCP tools in the gateway for services without an MCP server.
- Multi-node / horizontal scale (process-local worker registry, CSRF, rate
  limiter, MFA state — single-node by design, unchanged).
- Public signup, admin UI, `proxy.db` migration.

## Architecture

```
 Claude (phone / desktop / web)
   │ connector "Garmin":  https://<domain>/garmin/mcp   (own OAuth, own Bearer)
   │ connector "Rohlík":  https://<domain>/rohlik/mcp   (own OAuth, own Bearer)
   ▼
 [ Railway edge ]  ── TLS + domain (replaces operator-managed nginx)
   ▼
┌────────────────────── GATEWAY (one process, Python/Starlette) ──────────────────────┐
│                                                                                      │
│  Shared core (adapter-agnostic)                                                      │
│   · OAuth 2.1: DCR, authorize form scaffolding, PKCE S256, /token   — per prefix     │
│   · SQLite + AES-256-GCM store (volume at /data)                                     │
│   · security.py (CSRF, rate limits, headers), log.py, config.py                      │
│   · WorkerManager (used only by adapters with forward strategy B)                    │
│                                                                                      │
│  Adapter registry: { "garmin": GarminAdapter, "rohlik": RohlikAdapter }              │
│                                                                                      │
│   /garmin/*  ──► GarminAdapter                 /rohlik/*  ──► RohlikAdapter          │
│     login: garminconnect (+MFA)                  login: rhl email+password           │
│     verify: verify_tokens()                      verify: probe MCP call w/ headers   │
│     blob: garmin_tokens.json                     blob: {email, password}             │
│     forward B: ensure worker ──► 127.0.0.1:<port>  forward A: inject headers ──►     │
│                (unmodified garmin-mcp)                        mcp.rohlik.cz/mcp      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Routing (per adapter `<a>`, all registered from the adapter registry)

| Method | Path | Notes |
|---|---|---|
| GET | `/` | Landing page listing available connectors |
| GET | `/healthz` | Gateway liveness |
| GET | `/.well-known/oauth-authorization-server/<a>` | RFC 8414 path-scoped: issuer is `PUBLIC_URL/<a>` |
| GET | `/.well-known/oauth-protected-resource/<a>/mcp` | RFC 9728, resource = `PUBLIC_URL/<a>/mcp` |
| POST | `/<a>/oauth/register` | DCR (per-adapter `client_id`) |
| GET/POST | `/<a>/oauth/authorize` | Adapter-defined form (+ optional second-factor step) |
| POST | `/<a>/oauth/token` | Code exchange, PKCE verified |
| POST/GET/DELETE | `/<a>/mcp` | Authenticate Bearer → adapter's forward strategy |

Well-known routes live at the **domain root with the path suffix** (RFC 8414
inserts `/.well-known/...` between host and issuer path) — they are registered
centrally by the core, not under the prefix. Metadata URLs inside the documents
(`authorization_endpoint`, `token_endpoint`, `resource`) all carry the prefix.

### Adapter contract (the whole surface an adapter implements)

```python
class Adapter(Protocol):
    name: str                      # path prefix, DB `adapter` value, log field
    display_name: str              # landing page + authorize form title

    # -- credential acquisition (drives the shared authorize form) --
    form_fields: list[FormField]   # e.g. [email, password]; rendered by core
    async def start_login(self, fields: dict) -> LoginResult
        # LoginResult = Ok(blob: dict, account_key: str)
        #             | SecondFactor(state: Any, prompt: str)   # e.g. Garmin MFA
        #             | Failed(user_message: str)
    async def resume_login(self, state: Any, code: str) -> LoginResult
        # only reached after SecondFactor; core holds `state` in AuthState (TTL 300 s)
    async def verify(self, blob: dict) -> bool
        # gates persistence on EVERY authorize path (verify-then-persist invariant)

    # -- forwarding: exactly one of the two strategies --
    forward: RemoteForward | WorkerForward

@dataclass(frozen=True)
class RemoteForward:               # strategy A (Rohlik)
    upstream_url: str
    def headers(self, blob: dict) -> dict[str, str]   # e.g. rhl-email/rhl-pass

@dataclass(frozen=True)
class WorkerForward:               # strategy B (Garmin)
    def command(self, cfg: Config) -> list[str]        # e.g. GARMIN_MCP_CMD
    def env(self, blob: dict, workdir: Path, port: int) -> dict[str, str]
    def materialize(self, blob: dict, workdir: Path) -> None
        # write token files 0600 in 0700 dir; called before spawn
```

Notes:
- `account_key` = normalized lowercased login email (both current adapters).
  The worker registry key becomes the tuple `(adapter, account_key)`.
- The **core** owns: form rendering, CSRF, rate limits, OAuth params threading,
  MFA-state TTL, encryption of `blob`, code mint + redirect. The **adapter**
  owns: what the fields are, how login works, what the blob contains, how to
  reach the upstream.
- Secret-handling asymmetry is *adapter-defined and documented per adapter*:
  Garmin's blob holds only tokens (password discarded — unchanged invariant);
  Rohlik's blob necessarily holds email+password because the upstream wants
  them per-request. Gateway-wide invariant: nothing outside the encrypted blob
  is persisted; logs never carry blob contents.
- `RohlikAdapter.verify()` = one cheap MCP request (`initialize` or
  `tools/list`) against `ROHLIK_MCP_URL` with injected headers; non-2xx or
  auth error ⇒ re-render form. This is new behavior vs. the TS proxy.

## Data model + migration

```
accounts                            -- one row per (service, account)
  adapter      TEXT NOT NULL        -- 'garmin' | 'rohlik' | ...
  account_key  TEXT NOT NULL        -- normalized login email
  blob_enc     TEXT NOT NULL        -- AES-256-GCM(adapter-defined JSON)
  created_at, updated_at
  PRIMARY KEY (adapter, account_key)

access_tokens : + adapter TEXT NOT NULL      (token_hash stays PK)
oauth_clients : + adapter TEXT NOT NULL      (DCR happens per connector)
oauth_codes   : + adapter TEXT NOT NULL
tool_usage    : + adapter TEXT NOT NULL
```

- Crypto unchanged: key = SHA-256(`GATEWAY_SECRET`), `nonce:ciphertext` hex.
- **Migration** (one script, guarded by `PRAGMA user_version`):
  `garmin_accounts` → `accounts(adapter='garmin', account_key=garmin_user_key,
  blob_enc=garmin_tokens_enc)`; backfill `adapter='garmin'` in the other four
  tables; drop `garmin_accounts`. Ciphertext moves verbatim — no re-encryption.
  Tested against a fixture DB created with the old schema.

## Deployment (Railway)

- **One service**, built from the existing Dockerfile (gateway + pinned
  `garmin-mcp` in one image; workers spawn in-container — works the same on
  Railway). Add `tini` as ENTRYPOINT (replaces compose's `init: true` for
  reaping worker subprocesses).
- **Volume** mounted at `/data` (SQLite + per-user token dirs) ⇒ **single
  replica**, which the process-local state requires anyway.
- Env per environment: `PUBLIC_URL` = temporary `*.up.railway.app` URL on
  staging, the custom domain in production. All other env keys unchanged;
  `ROHLIK_MCP_URL` joins them.
- **Rollout order (risk-gated):**
  1. Staging service on the temporary URL, seeded with a scratch DB.
  2. **Spike test — the go/no-go gate:** full Garmin authorize (incl. MFA) from
     Railway egress IPs, worker spawn, one real tool call; watch for
     Cloudflare 429/challenge behavior. If Garmin blocks Railway egress and
     retries don't clear it, **stop: stay on the VPS** (the code consolidation
     still stands on its own).
  3. Rohlik adapter smoke test on staging (real login, one tool call).
  4. Point the custom domain at Railway; VPS keeps running until then.
  5. Staged re-onboarding of the circle (few accounts per day — Garmin
     Cloudflare limits are per-account; logins now also come from a new IP).
  6. Decommission VPS; archive `rohlik-oauth-proxy`; rename repo → `missingmcp`
     (package `garmin_gateway` → `missingmcp`, entrypoint `garmin-gateway` →
     `missingmcp`).
- **Backups:** Railway volume backups if available on the plan; otherwise a
  scheduled `sqlite3 .backup` shipped off-box. DB remains useless without
  `GATEWAY_SECRET` (kept outside Railway too). — *open question below.*

## Implementation order (in-place, tests green at every step)

1. **Extract the adapter seam** in this repo: introduce `Adapter` protocol +
   registry; move `garmin_login.py` and worker/env specifics behind
   `adapters/garmin/`; parameterize `WorkerManager` by `WorkerForward`;
   thread `adapter` through oauth.py/proxy.py signatures. Pure refactor —
   existing tests keep passing with path updates only.
2. **Schema migration** to `accounts` + `adapter` columns, with migration test.
3. **Path-scoped routing**: mount everything under `/<adapter>/`, path-scoped
   well-known endpoints, landing page lists connectors. Update tests' URLs.
4. **Rohlik adapter**: port ~200 lines from `rohlik-oauth-proxy/src/proxy.ts`
   + `oauth.ts` specifics (header names, validation, SSE forwarding quirks);
   add `verify()`; tests against a fake Rohlik upstream (extend
   `tests/conftest.py::fake_worker` pattern with a header-asserting stub).
5. **Railway staging + spike** (rollout order above).
6. **Flip + cleanup + rename** (last).

Steps 1–4 ship on the VPS-compatible codebase — nothing forces Railway before
the spike passes.

## Testing strategy

- Everything currently green stays green through step 1 (that is the point of
  in-place evolution).
- **Adapter contract tests**: one parametrized suite run against both adapters
  (login-ok / login-fail / verify-fail re-renders form / blob roundtrip).
  Garmin additionally: MFA paths (existing tests, relocated).
- **Rohlik forward**: fake upstream asserts injected headers, SSE pass-through,
  timeout → 504, upstream 5xx mapping. No real Rohlik in CI.
- **Migration**: old-schema fixture DB → migrate → data intact, `user_version`
  bumped, idempotent on re-run.
- **Isolation**: two adapters live in one process — a test that a Bearer token
  minted for `/rohlik/mcp` is rejected on `/garmin/mcp` (the `adapter` column
  on `access_tokens` is the enforcement point).
- **Manual release gates** (unchanged philosophy): real-account smoke on
  staging for both adapters; the Garmin one incl. MFA is the Railway go/no-go.

## Error handling

Unchanged from the 2026-06-30 spec for Garmin (worker start-failure → 502,
timeout → 504, body limit → 413, rate limits → 429). Strategy A adds:
upstream unreachable/timeout → 504, upstream 401/403 (credentials went stale)
→ surface "reconnect Rohlík" the same way worker-start-failure does for Garmin.

## Open questions / future

- **Backup mechanism on Railway** — volume snapshot support vs. scheduled
  `.backup` push to object storage. Decide during step 5; requirement is only
  "off-box copy of /data + GATEWAY_SECRET stored elsewhere".
- Whether `scripts/` (status, monitor, revoke, usage, health) grow an
  `--adapter` filter in step 2 or stay Garmin-shaped until after the flip.
- Third adapter candidates (would validate the seam, none committed): any
  service with either a hosted MCP needing header auth, or a single-user
  HTTP-transport MCP CLI.
