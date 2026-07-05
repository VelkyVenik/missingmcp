# Path-Scoped Routing (`/garmin/mcp`) — Implementation Plan (Plan A2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Garmin connector from the bare `/mcp` to `/garmin/mcp` with correct path-scoped OAuth discovery (RFC 8414 + RFC 9728), registered per-adapter from the registry — spec Part 2 — so a second adapter later mounts under its own prefix with no core change.

**Architecture:** `oauth.metadata` becomes adapter-scoped and a new `protected_resource_metadata` is added; both well-known documents carry the `/garmin` prefix. `build_app` registers each registered adapter's routes (`/<a>/oauth/*`, `/<a>/mcp`, and the two root-level path-scoped `.well-known` routes) in a loop over the registry. The login/MFA templates get a per-adapter form action. **No `/mcp` alias** — the old root routes are removed, so the staging connector must be re-added (verified by a discovery spike). Home-page restructure, revoke UX, and backups are Plan B.

**Tech Stack:** Python 3.12, Starlette, sqlite3, pytest via `uv run --extra dev pytest`.

## Global Constraints

- **Behavior within the flow is unchanged; only the URL surface moves.** Same OAuth 2.1 (DCR → authorize + Garmin login/MFA → token → PKCE S256), same HTML, same security headers, same structured-log events. What changes: every endpoint gains the `/garmin` prefix, discovery is path-scoped, and there is **no `/mcp` alias** (the bare `/mcp`, `/oauth/*`, and root `/.well-known/oauth-authorization-server` are removed).
- **Path-scoped discovery (the one real risk).** For the resource `PUBLIC_URL/garmin/mcp`: RFC 9728 doc at `/.well-known/oauth-protected-resource/garmin/mcp` with `resource = PUBLIC_URL/garmin/mcp` and `authorization_servers = [PUBLIC_URL/garmin]`; RFC 8414 doc at `/.well-known/oauth-authorization-server/garmin` with `issuer = PUBLIC_URL/garmin` and all endpoints prefixed. Both are built now. The well-known routes live at the **domain root with the path suffix** (not under `/garmin/`), registered centrally.
- **CSP has no `form-action` directive and must stay that way** — `security.py:14-24` documents why (the login form POSTs, then the server 302s to the client's arbitrary `redirect_uri`; `form-action 'self'` would break that cross-origin redirect). Do NOT add a `form-action` directive when touching templates/routing.
- **Single worker manager** (one worker-based adapter today: garmin). A second, non-worker adapter (Rohlik, mode A) reworks forwarding entirely — that is step 4, out of scope here.
- Test command: `uv run --extra dev pytest -q` (the `--extra dev` is REQUIRED). **Baseline before Task 1: 91 passed.** Run the full suite at the end of every task; green before each commit.
- `garmin_user_key` is gone (Plan A1). `account_key`, `(adapter, account_key)`, and adapter-aware store CRUD are in place.
- Python 3.12; source under `src/garmin_gateway/`, tests under `tests/`.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (repo commits as `vaclav@slajs.eu`).
- **Out of scope (do NOT do here):** the MissingMCP rozcestník home page + `/garmin` subpage (Plan B — this plan only minimally fixes the landing's connect URL so it isn't wrong during the spike); revoke/status UX; Litestream backups; Rohlik / `RemoteForward`; per-adapter worker managers.

## File Structure

```
src/garmin_gateway/
  oauth.py    — metadata(config, adapter) prefixed + new protected_resource_metadata(config, adapter);
                render sites fill a per-adapter {AUTHORIZE_ACTION}
  app.py      — routes registered per adapter in a loop: /<a>/oauth/*, /<a>/mcp, and the two
                path-scoped .well-known routes; root /mcp, /oauth/*, root well-known removed;
                landing connect URL → /garmin/mcp
  templates/
    authorize.html, mfa.html — form action "/oauth/authorize" → "{AUTHORIZE_ACTION}"
    landing.html             — {PUBLIC_URL}/mcp → {PUBLIC_URL}/garmin/mcp (minimal keep-working)
tests/
  test_oauth.py — metadata test → adapter-scoped + prefixed; authorize-get asserts the /garmin action
  test_app.py   — route URLs → /garmin/*; add protected-resource endpoint test
```

---

### Task 1: Adapter-scoped metadata + protected-resource metadata

**Files:**
- Modify: `src/garmin_gateway/oauth.py` (`metadata`, add `protected_resource_metadata`)
- Modify: `tests/test_oauth.py` (`test_metadata_shape` + a new protected-resource test)

**Interfaces produced:**
- `oauth.metadata(config, adapter) -> dict` — `issuer = f"{config.public_url}/{adapter.name}"`, endpoints prefixed.
- `oauth.protected_resource_metadata(config, adapter) -> dict` — `{"resource": f"{config.public_url}/{adapter.name}/mcp", "authorization_servers": [f"{config.public_url}/{adapter.name}"]}`.

- [ ] **Step 1: Update the metadata tests (they encode the new shape)**

In `tests/test_oauth.py`, replace `test_metadata_shape` and add a protected-resource test. (`ADAPTER = GarminAdapter(CONFIG)` and `CONFIG` already exist in the file.)

```python
def test_metadata_shape():
    m = oauth.metadata(CONFIG, ADAPTER)
    assert m["issuer"] == "https://gw.example.com/garmin"
    assert m["authorization_endpoint"] == "https://gw.example.com/garmin/oauth/authorize"
    assert m["token_endpoint"] == "https://gw.example.com/garmin/oauth/token"
    assert m["registration_endpoint"] == "https://gw.example.com/garmin/oauth/register"
    assert m["code_challenge_methods_supported"] == ["S256"]


def test_protected_resource_metadata_shape():
    m = oauth.protected_resource_metadata(CONFIG, ADAPTER)
    assert m["resource"] == "https://gw.example.com/garmin/mcp"
    assert m["authorization_servers"] == ["https://gw.example.com/garmin"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_oauth.py::test_metadata_shape tests/test_oauth.py::test_protected_resource_metadata_shape -v`
Expected: FAIL — `test_metadata_shape` fails on the un-prefixed issuer; `test_protected_resource_metadata_shape` fails with `AttributeError: module ... has no attribute 'protected_resource_metadata'`.

- [ ] **Step 3: Implement in `oauth.py`**

Replace `metadata` (oauth.py:16-27) with:

```python
def metadata(config, adapter) -> dict:
    base = f"{config.public_url}/{adapter.name}"
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


def protected_resource_metadata(config, adapter) -> dict:
    # RFC 9728: points the MCP client at this resource's authorization server.
    base = config.public_url
    return {
        "resource": f"{base}/{adapter.name}/mcp",
        "authorization_servers": [f"{base}/{adapter.name}"],
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --extra dev pytest tests/test_oauth.py::test_metadata_shape tests/test_oauth.py::test_protected_resource_metadata_shape -v`
Expected: 2 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: **92 passed** (91 + 1 net new: the protected-resource test; `test_metadata_shape` was modified, not added).

- [ ] **Step 6: Commit**

```bash
git add src/garmin_gateway/oauth.py tests/test_oauth.py
git commit -m "feat(oauth): adapter-scoped metadata + RFC 9728 protected-resource metadata

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Per-adapter form action in the login/MFA templates

The login and MFA forms hard-code `action="/oauth/authorize"`. Under path routing they must POST to `/garmin/oauth/authorize`. Make the action a per-adapter placeholder filled at render time.

**Files:**
- Modify: `src/garmin_gateway/templates/authorize.html` (form action)
- Modify: `src/garmin_gateway/templates/mfa.html` (form action)
- Modify: `src/garmin_gateway/oauth.py` (`render_authorize` + the two MFA render sites fill `AUTHORIZE_ACTION`)
- Modify: `tests/test_oauth.py` (strengthen `test_authorize_get_renders_form`)

**Interfaces:** none new (internal render change).

- [ ] **Step 1: Strengthen the authorize-get test to assert the prefixed action**

In `tests/test_oauth.py`, `test_authorize_get_renders_form`, add after the existing asserts:
```python
    assert 'action="/garmin/oauth/authorize"' in r.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_oauth.py::test_authorize_get_renders_form -v`
Expected: FAIL — the template still renders `action="/oauth/authorize"`.

- [ ] **Step 3: Add the placeholder to both templates**

`src/garmin_gateway/templates/authorize.html` line 26:
```html
<form method="post" action="{AUTHORIZE_ACTION}">
```
`src/garmin_gateway/templates/mfa.html` line 17:
```html
<form method="post" action="{AUTHORIZE_ACTION}">
```

- [ ] **Step 4: Fill `AUTHORIZE_ACTION` at every render site in `oauth.py`**

`render_authorize` (oauth.py:99-109) — add the field to the mapping:
```python
def render_authorize(params: dict, csrf_token: str, config, adapter, error: str = "") -> HTMLResponse:
    body = _fill(_tpl(adapter.authorize_template), {
        "CSRF": csrf_token,
        "CLIENT_ID": params.get("client_id", ""),
        "REDIRECT_URI": params.get("redirect_uri", ""),
        "STATE": params.get("state", ""),
        "CODE_CHALLENGE": params.get("code_challenge", ""),
        "METHOD": params.get("code_challenge_method", ""),
        "AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize",
        **_operator_fields(config),
    }, error)
    return HTMLResponse(body)
```

Both MFA render sites in `authorize_post` (the wrong-code re-prompt ~oauth.py:173-175 and the initial MFA page ~oauth.py:205-206) — add `"AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize"` to each `_fill` mapping:
```python
            body = _fill(_tpl(adapter.second_factor_template),
                         {"CSRF": state.csrf.issue(), "LOGIN_ID": lid,
                          "AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize",
                          **_operator_fields(config)},
                         str(e))
```
```python
        body = _fill(_tpl(adapter.second_factor_template),
                     {"CSRF": state.csrf.issue(), "LOGIN_ID": lid,
                      "AUTHORIZE_ACTION": f"/{adapter.name}/oauth/authorize",
                      **_operator_fields(config)}, "")
```

- [ ] **Step 5: Run the authorize-get test, then the full suite**

Run: `uv run --extra dev pytest tests/test_oauth.py::test_authorize_get_renders_form -v` → PASS.
Run: `uv run --extra dev pytest -q` → **92 passed** (assertion added to an existing test; no count change).

(The MFA-path tests `test_login_mfa_then_verify_redirects` / `test_mfa_wrong_code_reprompts` render `mfa.html` with the new placeholder; `_fill` leaves no `{AUTHORIZE_ACTION}` behind, and those tests assert on `login_id` / form presence, so they stay green.)

- [ ] **Step 6: Commit**

```bash
git add src/garmin_gateway/templates/authorize.html src/garmin_gateway/templates/mfa.html src/garmin_gateway/oauth.py tests/test_oauth.py
git commit -m "feat(oauth): per-adapter form action so login/MFA POST to /<adapter>/oauth/authorize

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Path-scoped routing in `build_app` + landing URL fix

The core routing change: register each adapter's routes under its prefix + the two path-scoped well-known routes; remove the root `/mcp`, `/oauth/*`, and root `/.well-known/oauth-authorization-server`.

**Files:**
- Modify: `src/garmin_gateway/app.py` (route registration; landing string already replaces `{PUBLIC_URL}`)
- Modify: `src/garmin_gateway/templates/landing.html` (connect URL → `/garmin/mcp`)
- Modify: `tests/test_app.py` (route URLs + a protected-resource endpoint test)

**Interfaces:**
- Consumes: `oauth.metadata(config, adapter)`, `oauth.protected_resource_metadata(config, adapter)` (Task 1); `build_adapters(config)`, `adapter.name`, `adapter.forward`.
- Produces: routes `/<a>/oauth/register|authorize|token`, `/<a>/mcp` (POST/GET/DELETE), `/.well-known/oauth-authorization-server/<a>`, `/.well-known/oauth-protected-resource/<a>/mcp`.

- [ ] **Step 1: Update `test_app.py` to the path-scoped URLs**

Replace the route-dependent tests:

```python
def test_landing_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "/garmin/mcp" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_healthz(tmp_path):
    c = _client(tmp_path)
    assert c.get("/healthz").text == "ok"


def test_metadata_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-authorization-server/garmin").json()
    assert m["issuer"] == "https://gw.example.com/garmin"
    assert m["authorization_endpoint"] == "https://gw.example.com/garmin/oauth/authorize"


def test_protected_resource_endpoint(tmp_path):
    c = _client(tmp_path)
    m = c.get("/.well-known/oauth-protected-resource/garmin/mcp").json()
    assert m["resource"] == "https://gw.example.com/garmin/mcp"
    assert m["authorization_servers"] == ["https://gw.example.com/garmin"]


def test_mcp_requires_auth(tmp_path):
    c = _client(tmp_path)
    assert c.post("/garmin/mcp", json={}).status_code == 401
    assert c.post("/mcp", json={}).status_code == 404   # no alias — old path is gone
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_app.py -v`
Expected: FAIL — `test_metadata_endpoint`/`test_protected_resource_endpoint`/`test_mcp_requires_auth` hit paths that don't exist yet (404 where 200/401 expected), and the old routes still exist.

- [ ] **Step 3: Rewrite route registration in `build_app`**

In `src/garmin_gateway/app.py`, replace the per-endpoint handler defs (`meta`, `register`, `authz_get`, `authz_post`, `token`, `mcp`) and the `routes = [...]` list with a per-adapter factory + loop. Keep `home`, `notfound`, `healthz`, `favicon`, the landing string, `auth_state`, `rate`, `conn`, and the lifespan as they are. Manager stays single (one worker adapter today).

The construction block becomes:
```python
    conn = store.init_db(config.db_path)
    adapters = build_adapters(config)
    garmin = adapters["garmin"]
    # Single worker manager: garmin is the only worker-based adapter today. A
    # second, non-worker adapter (step 4) reworks forwarding and revisits this.
    manager = WorkerManager(config, garmin.forward)
    auth_state = oauth.AuthState(security.CsrfStore())
    rate = security.RateLimiter()
```

(`home`, `notfound`, `favicon`, `healthz` defs unchanged.)

Add the per-adapter route factory (place it after the `favicon`/`healthz` defs, before the lifespan):
```python
    def adapter_routes(adapter):
        p = adapter.name

        async def meta(request):
            return JSONResponse(oauth.metadata(config, adapter))

        async def prmeta(request):
            return JSONResponse(oauth.protected_resource_metadata(config, adapter))

        async def register(request):
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return JSONResponse({"error": "rate_limited"}, status_code=429)
            return await oauth.register_client(request, conn, adapter)

        async def authz_get(request):
            return await oauth.authorize_get(request, adapter, auth_state, conn, config)

        async def authz_post(request):
            if not rate.check(f"login:{request.client.host}", 5, 60):
                return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
            return await oauth.authorize_post(request, adapter, auth_state, conn, config)

        async def token(request):
            if not rate.check(f"oauth:{request.client.host}", 20, 60):
                return JSONResponse({"error": "rate_limited"}, status_code=429)
            return await oauth.token_exchange(request, conn, config)

        def mcp(method):
            async def handler(request):
                return await proxy.handle_mcp(request, method, adapter, conn, manager,
                                              config, config.gateway_secret, rate)
            return handler

        return [
            Route(f"/.well-known/oauth-authorization-server/{p}", meta, methods=["GET"]),
            Route(f"/.well-known/oauth-protected-resource/{p}/mcp", prmeta, methods=["GET"]),
            Route(f"/{p}/oauth/register", register, methods=["POST"]),
            Route(f"/{p}/oauth/authorize", authz_get, methods=["GET"]),
            Route(f"/{p}/oauth/authorize", authz_post, methods=["POST"]),
            Route(f"/{p}/oauth/token", token, methods=["POST"]),
            Route(f"/{p}/mcp", mcp("POST"), methods=["POST"]),
            Route(f"/{p}/mcp", mcp("GET"), methods=["GET"]),
            Route(f"/{p}/mcp", mcp("DELETE"), methods=["DELETE"]),
        ]
```

Replace the `routes = [...]` list with:
```python
    routes = [
        Route("/", home, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/favicon.svg", favicon, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
    ]
    for a in adapters.values():
        routes.extend(adapter_routes(a))
    # Catch-all (must stay last): unknown GET paths get the landing page.
    routes.append(Route("/{path:path}", notfound, methods=["GET"]))
```

(The lifespan still references the single `manager` — no change.)

- [ ] **Step 4: Fix the landing connect URL**

`src/garmin_gateway/templates/landing.html` — change both `{PUBLIC_URL}/mcp` occurrences to `{PUBLIC_URL}/garmin/mcp` (the "Set the Server URL to" line and the `claude mcp add … {PUBLIC_URL}/mcp` CLI example). Leave everything else (the rozcestník restructure is Plan B).

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: **93 passed** (92 + `test_protected_resource_endpoint`). If a test still references a bare `/mcp` or `/oauth/*` route and 404s unexpectedly, that's the expected removal — update the test to the `/garmin/*` path (do not re-add an alias).

- [ ] **Step 6: Local boot smoke (no Garmin needed)**

```bash
cd /Users/vaclav.slajs/dev/garmin-mcp-gateway
GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 \
  DATA_DIR=./.localdata uv run garmin-gateway &
sleep 2
curl -s http://localhost:8088/.well-known/oauth-protected-resource/garmin/mcp | python3 -m json.tool
curl -s http://localhost:8088/.well-known/oauth-authorization-server/garmin | python3 -c "import sys,json;print(json.load(sys.stdin)['issuer'])"
curl -s -o /dev/null -w "garmin/mcp unauth = %{http_code}\n" -X POST http://localhost:8088/garmin/mcp
curl -s -o /dev/null -w "old /mcp = %{http_code}\n" -X POST http://localhost:8088/mcp
kill %1
```
Expected: protected-resource JSON with `resource` ending `/garmin/mcp`; issuer `http://localhost:8088/garmin`; `garmin/mcp unauth = 401`; `old /mcp = 404`.

- [ ] **Step 7: Commit**

```bash
git add src/garmin_gateway/app.py src/garmin_gateway/templates/landing.html tests/test_app.py
git commit -m "feat(app): path-scoped routing — /garmin/mcp + path-scoped .well-known, no /mcp alias

Routes registered per adapter from the registry; RFC 8414 + RFC 9728 discovery
docs carry the /garmin prefix; bare /mcp and /oauth/* removed. Landing connect
URL updated to /garmin/mcp.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Docs — CLAUDE.md routing + spec Part 2 tick

**Files:**
- Modify: `CLAUDE.md` (endpoint table / request-flow mention of the prefix)
- Modify: `docs/superpowers/specs/2026-07-05-garmin-finish-and-home-design.md` (tick Part 2 / the routing item in the implementation order)

**Interfaces:** none.

- [ ] **Step 1: Update CLAUDE.md**

In the request-flow / OAuth section of `CLAUDE.md`, update any `/mcp`, `/oauth/…`, `/.well-known/oauth-authorization-server` reference to the path-scoped form. Add a line to the "Cross-cutting invariants" (or the OAuth module note):
```markdown
- **Path-scoped connectors:** each adapter is mounted under `/<adapter>` — the connector is `/<adapter>/mcp` (e.g. `/garmin/mcp`), OAuth endpoints are `/<adapter>/oauth/*`, and discovery is path-scoped: `/.well-known/oauth-authorization-server/<adapter>` (RFC 8414, issuer `PUBLIC_URL/<adapter>`) and `/.well-known/oauth-protected-resource/<adapter>/mcp` (RFC 9728). There is no bare `/mcp` alias.
```

- [ ] **Step 2: Tick Part 2 in the spec**

In `docs/superpowers/specs/2026-07-05-garmin-finish-and-home-design.md`, in the "Implementation order — two plans" section, mark Plan A's Part 2 done, e.g. change the `**Plan A (structural, breaking):**` line to note `Part 1 ✅ (plan 2026-07-05-schema-migration.md), Part 2 ✅ (plan 2026-07-05-path-routing.md) — pending the staging discovery spike`.

- [ ] **Step 3: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: **93 passed**.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-07-05-garmin-finish-and-home-design.md
git commit -m "docs: path-scoped connector routing in CLAUDE.md; spec Part 2 done

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Release gate — staging discovery spike (manual, after merge + deploy)

This is the go/no-go for the whole plan, mirroring the Garmin-login spike. Run it after merging to `main` and redeploying staging:

1. Redeploy staging (`railway up --detach` from the repo root); wait for `SUCCESS`.
2. Confirm the new discovery docs serve on staging:
   `curl -s https://gateway-production-720e.up.railway.app/.well-known/oauth-protected-resource/garmin/mcp` → JSON with `resource` ending `/garmin/mcp`.
3. **In a Claude client, add the connector fresh at `https://gateway-production-720e.up.railway.app/garmin/mcp`.** Confirm Claude's OAuth discovery resolves (DCR → authorize form), sign in with the Garmin account (re-login the migrated `vaclav@slajs.eu` — `upsert` overwrites its tokens), complete MFA, and run one tool call.
4. Watch the logs for the full chain: `register`, `login-start` → `login-start-result`, `authorize-finish`, `worker-spawn`/`worker-started`, `mcp-request adapter="garmin"`.

**If discovery fails** (Claude can't find the auth server from the path-scoped well-known): the fallback is a root `/.well-known/oauth-authorization-server` (and/or `/.well-known/oauth-protected-resource`) that points at the garmin issuer — designed and added only if the spike shows it's needed. Do not build it preemptively.

## Self-review notes (author)

- Spec Part 2 coverage: `/garmin/mcp` + `/garmin/oauth/*` ✓ (Task 3), RFC 8414 path-scoped ✓ (Tasks 1+3), RFC 9728 path-scoped ✓ (Tasks 1+3), no `/mcp` alias ✓ (Task 3 removes it + `test_mcp_requires_auth` asserts 404), registry-driven registration ✓ (Task 3 loop), template action ✓ (Task 2), landing keep-working ✓ (Task 3), discovery spike ✓ (release gate).
- Test-count arithmetic: 91 → Task 1 +1 (92) → Task 2 +0 (92) → Task 3 +1 (93) → Task 4 +0 (93).
- CSP `form-action` absence preserved (constraint restated; templates change only the action attribute, not headers).
- Type consistency: `metadata(config, adapter)` and `protected_resource_metadata(config, adapter)` defined in Task 1 are called with exactly those args in Task 3's handlers; `adapter.name` used consistently for the prefix everywhere.
- The oauth.py authorize/token/register signatures are unchanged from Plan A1 (they already take `adapter` / read it from the code row) — Task 3 only changes where they're mounted, not their signatures.
