# garmin_mcp bump: 2974244b → e8554bcd

Status: shipped & smoke-tested (PR #27 merged 2026-09-03, deploy ca59569
verified live; Václav confirmed live usage works the same day)
Date: 2026-09-03

## Why

Users keep asking whether we track upstream `garmin_mcp`. The pin was
2026-06-23; upstream moved 36 commits (through 2026-09-01): 17 new tools
(131 → 148) and fixes users have hit directly — wrong weather/lactate-threshold
units, `get_training_load_trend` returning None for CTL/ATL, VO2 max trend
mismatching Connect, strength workouts losing set counts, null-handling in
HRV/sleep/body battery. Plus per-call 90s bounds inside the worker
(`GARMIN_MCP_CALL_TIMEOUT`), so a stalled Garmin request can't hang it.

## Supply-chain review (2026-09-03)

Full diff reviewed (agent-assisted, verdict SAFE TO PIN): no new packages
(`mcp>=1.28.1,<2` is the only dependency change — compatible with our own
`"mcp<2"` install pin), no new network destinations, no code execution or
obfuscation, token handling changes are hardening (chmod 0700/0600, `${HOME}`
resolution). Entrypoint and `GARMIN_MCP_*`/`GARMINTOKENS` env contract
unchanged; `GARMIN_MCP_HOST` default flipped to `127.0.0.1` (we set it
explicitly anyway).

## The one behavioral change that needed gateway work

Upstream moved the Garmin login to a background thread (their issue #255): the
worker now answers `/healthz` **before** the sign-in resolves, and stale tokens
no longer make it exit cleanly during startup — the signal our
`WorkerCredentialsRejected` → re-auth-401 self-heal keyed on. Without
adaptation, a stale account would get per-call "run 'garmin-mcp-auth'" tool
errors forever and the OAuth re-auth flow would never trigger.

**Fix (this PR): the login gate.** The worker output pump classifies the
worker's sign-in log lines (`GarminWorkerForward.login_outcome`, optional on
the WorkerForward protocol) into a per-spawn `LoginGate`; `ensure_worker`
blocks on it after `/healthz`, inside the unchanged `worker_startup_timeout`
budget. "failed" → `worker-login-rejected` + `WorkerCredentialsRejected`
(re-auth, routine; added to daily_triage `_SELF_HEAL_EVENTS`); silence →
`worker-login-timeout` + plain `WorkerStartError` (stays loud). The old
clean-exit signal remains handled for older pins. Details:
`docs/architecture.md` → workers.py → Login gate.

## Release gate

Manual smoke test after deploy: Garmin login (+ MFA), a few tool calls, and
specifically the stale-token path — corrupt/expire a test account's blob and
verify the client re-runs OAuth instead of surfacing worker tool errors.
