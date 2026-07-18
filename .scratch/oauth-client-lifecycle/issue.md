# OAuth client & upstream-token lifecycle — follow-ups

Status: ready-for-agent

Two related robustness gaps found while triaging the 2026-07-17 sign-in
reports (see git: `6cd59ae` CSRF dead-end fix, `bcf7ae8` orphan-TTL fix).

**#2 DONE (2026-07-18)** — dead upstream credentials now return a re-auth 401
with the RFC 9728 challenge instead of a 502; see the resolution note under §2.
Only §1 (last_seen orphan sweep) remains open.

## 1. Sweep orphan clients by last activity, not creation age

`store.cleanup_orphan_clients` deletes clients with **no live token** older
than `orphan_client_ttl` (now 30 days) **since creation**. A long-lived cached
client (Claude org) whose user pauses >90 days (access-token TTL) becomes
0-token and older than any creation-age cutoff → swept → "unknown client_id"
on their return.

Fix: add `last_seen` to `oauth_clients` (schema `user_version` 1→2 migration,
following the existing guarded-migration pattern), update it on every
authorize/token use, and sweep on `last_seen < cutoff` instead of
`created_at`. Keeps scanner-spam bounded while never racing a real client.

## 2. Return 401 (not 502) when upstream tokens are dead

When a worker can't start because the account's Garmin tokens are gone
("OAuth tokens not found" — 131 occurrences for 3 accounts in the 07-12..17
logs), the proxy returns 502 `garmin_session_expired`. MCP clients don't
recover from 502 — the user retries forever (56× for one account).

A 401 with the RFC 9728 challenge would make Claude re-run authorization
automatically — the user re-signs in with two clicks and self-heals.
Touches a documented invariant (CLAUDE.md: worker start-failure→502
`<adapter>_session_expired`), so: change deliberately, update CLAUDE.md,
keep the log events' names/values stable, and cover with tests
(worker path + local path `SessionExpired` + remote path 401/403 mapping
should all become the same 401 shape).

### Resolution (2026-07-18)

`proxy._reauth_required(config, adapter)` replaces the old `_session_expired`
502 helper. All four "credentials stale/gone" exits now return **401** with
`WWW-Authenticate: Bearer error="invalid_token", …, resource_metadata="<PUBLIC_URL>/.well-known/oauth-protected-resource/<adapter>/mcp"`
and body `{"error": "invalid_token", "message": "Your <X> session expired…"}`:

- worker start failure (event `worker-start-failed`)
- local `SessionExpired` (event `local-forward-auth-stale`)
- remote upstream 401/403 (event `remote-forward-auth-stale`)
- missing account blob (`get_account_tokens` → None) — was a bare 401
  `unknown_account`, now carries the challenge too, for the same self-heal.

Log event names/values unchanged. CLAUDE.md `proxy.py` bullet updated. Stale
docstrings in `adapters/base.py::SessionExpired` and `adapters/whoop/api.py::
WhoopAuthError` corrected. Tests: `test_proxy.py::test_worker_start_failure_maps_to_reauth_401`
+ `::test_unknown_account_maps_to_reauth_401`, `test_remote_forward.py::
test_upstream_auth_rejection_maps_to_reauth_401`, `test_local_forward.py::
test_session_expired_maps_to_reauth_401`, `test_whoop_e2e.py::
test_stale_refresh_maps_to_reauth_401` (full-middleware, confirms the header
survives). Full suite 256 passed.
