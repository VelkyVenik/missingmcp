# Self-service `/account` + read-only mode — design

Date: 2026-07-05
Status: approved (brainstorming); pending implementation plan

## Goal

Let a connected user manage their own account without the operator, and without the
gateway ever handling passwords. Two capabilities:

1. **Read-only mode** — opt-in; the account can then only *read* Garmin data, never
   write/delete.
2. **Disconnect / Delete** — self-service removal of access (and optionally all data).

Authentication reuses the fact that the user is **already authenticated in Claude**
(they hold a Bearer token). No passwords, no email infra, no Cloudflare Access
seats — so it scales past the free 50-seat limit as the user base grows.

## Decisions (from brainstorming)

- **Default mode: full access.** Read-only is opt-in (existing users keep write).
- **Two removal actions:** *Disconnect* (revoke access tokens, keep stored Garmin
  tokens → fast reconnect) and *Delete everything* (access tokens + Garmin tokens +
  usage rows → full "forget me").
- **Auth = Claude tool link.** A gateway-injected `manage_account` tool returns a
  short-lived signed link tied to the account; clicking it opens an authenticated
  session on `/account`.
- **Read-only "write" detection = denylist of prefixes:** `create_ set_ add_
  delete_ update_ upload_ log_ schedule_ unschedule_ remove_ upsert`. Everything
  else (`get_ count_ download_ request_reload`) is read and allowed.
- **Lifetimes:** link token 10 min, session cookie 30 min.

## Data model

- `garmin_accounts` gains `read_only INTEGER NOT NULL DEFAULT 0` (0 = full, 1 =
  read-only). Added via `ALTER TABLE … ADD COLUMN` migration in `init_db` (same
  pattern as `access_tokens.expires_at`; backward compatible — existing rows = full).
- **Stateless session tokens** (no new table): HMAC-SHA256 signed with
  `GATEWAY_SECRET`.
  - `sign_account_token(key, ttl) -> str` — payload `{k: garmin_user_key, exp: epoch}`,
    urlsafe-base64 + `.` + hex HMAC.
  - `verify_account_token(token) -> str | None` — constant-time HMAC check
    (`hmac.compare_digest`) + expiry; returns the account key or None.
  - Both the link token (ttl 600 s) and the session cookie (ttl 1800 s) use this.

## Components

### 1. MCP rewrite in the proxy (`proxy.py` + a small helper)
`handle_mcp` already parses the JSON-RPC method / `tools/call` name (for usage).
Extend it:

- **`tools/call` name = `manage_account`** → intercept: generate
  `{PUBLIC_URL}/account?t=<link token>` and return a JSON-RPC tool result
  (`result.content[0].text`) echoing the request `id`. Do **not** forward upstream.
- **`tools/call` write tool + account is read-only** → intercept: return a JSON-RPC
  *tool result* (not a protocol error) whose text explains the account is read-only
  and writes are disabled (with the `/account` hint), echoing the request `id`. Do
  **not** forward. Defense-in-depth in case Claude cached an old tool list.
- **`tools/list`** → forward, then **rewrite the response**: add the
  `manage_account` tool descriptor, and if the account is read-only, drop write
  tools (denylist). Must handle both response encodings FastMCP may use:
  - `application/json` → parse JSON, edit `result.tools`, re-serialize.
  - `text/event-stream` → parse the SSE `data:` frame(s), edit the JSON-RPC payload,
    re-emit as SSE. Buffer the (bounded, ~130-tool) response rather than streaming it.
- **Everything else** → transparent passthrough exactly as today (still streamed).

`manage_account` tool descriptor: `{name, description: "Get a link to manage your
Garmin gateway account — read-only mode, disconnect, or delete your data.",
inputSchema: {type: object, properties: {}}}`.

### 2. `/account` page + session (`app.py` routes, `oauth.py`/new module, template)
- `GET /account?t=<token>` → `verify_account_token` → set signed session cookie
  `gw_account` (HttpOnly, Secure, SameSite=Lax, Max-Age 1800) → render page.
- `GET /account` (valid cookie) → render page.
- No/invalid token/cookie → a page explaining "open the link from Claude" (no data,
  no actions).
- Page shows: account key, **read-only toggle**, **Disconnect** button, **Delete
  everything** button (with an explicit confirm). Layout leaves room for future items.
- `POST /account/mode` (toggle), `POST /account/disconnect`, `POST /account/delete`
  — all require a **CSRF token** (existing `CsrfStore`) and re-check the session.

### 3. Store helpers (`store.py`)
- `set_read_only(conn, key, on: bool)`, `is_read_only(conn, key) -> bool`.
- `disconnect_account(conn, key)` → reuse `revoke_account` (access tokens only).
- `delete_account(conn, key)` → delete from `access_tokens`, `garmin_accounts`,
  `tool_usage` (full removal). Worker for that key is reaped on next cycle / can be
  terminated.

## Data flow

```
Claude → tools/list        → gateway rewrites (add manage_account, filter writes if RO)
User: "manage my account"  → tools/call manage_account → gateway returns …/account?t=TOKEN
User clicks link           → GET /account?t=TOKEN → verify → set cookie → page
User toggles RO / removes  → POST (+CSRF) → store update → confirmation
Later MCP calls            → read-only account: write tools hidden + write calls refused
```

## Security

- Session tokens HMAC-signed with `GATEWAY_SECRET`, constant-time verify, short TTLs.
- Cookie HttpOnly + Secure + SameSite=Lax; destructive actions POST + CSRF only.
- Link token is single-use in spirit (10-min window); it appears only in the user's
  own Claude tool result.
- CSP unchanged (`default-src 'self'`); the account page is same-origin HTML.
- `manage_account` is the only gateway-level tool; all Garmin tools still forward
  unmodified. Read-only enforced at both `tools/list` and `tools/call`.

## Testing

- `sign_account_token` / `verify_account_token`: round-trip, tamper, expiry.
- Store: `set_read_only`/`is_read_only`, `delete_account` removes all three tables,
  `disconnect_account` keeps Garmin tokens.
- MCP rewrite: `tools/list` gets `manage_account` added; read-only drops write tools;
  works for both JSON and SSE encodings.
- `tools/call manage_account` intercepted → returns a link, not forwarded.
- Read-only account: write `tools/call` refused; read tool passes.
- `/account`: invalid/expired token → no session; valid token → cookie + page;
  toggle / disconnect / delete happy paths; POST without CSRF rejected.

## Out of scope (future)
- Admin panel (operator-wide) — separate, could use Cloudflare Access (1 seat).
- Per-tool granular permissions beyond read/write.
- Email notifications on account changes.
