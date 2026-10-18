# 01 — Land upstream /readyz, bump the pin, swap the login gate onto it

Type: task
Status: resolved (2026-10-12 — PR #48: pin e79df6f, gate on /readyz)
Blocked by: — (upstream Taxuspt/garmin_mcp#380 landed as merge commit `8e809d8` on 2026-10-12)

## Why

`workers.py`'s login gate learns the worker's Garmin sign-in outcome by
parsing its stderr lines (`_LOGIN_OK_LINE` / "failed to initialize" in
`adapters/garmin/__init__.py`). Any upstream wording change silently breaks
the re-auth self-heal. Upstream PR #381 (opened 2026-10-05 from
`VelkyVenik/garmin_mcp`, branch `feat/readyz-login-state`) adds
`GET /readyz` → `200 {"status":"ready"}` / `503 {"status":"pending"|"failed"}`.

## Steps once it lands

1. **Watch for the merge.** The maintainer never merges external PRs
   directly; he re-applies them in his own PR ("Closes #381 via clean apply",
   batches roughly every 1–2 weeks) and closes ours. Check
   `gh pr list -R Taxuspt/garmin_mcp --search 381 --state all` and
   `git log origin/main --grep readyz` in a clone. If it's still open after
   ~3 weeks, nudge on the issue (show the text to the operator first).
2. **Bump `GARMIN_MCP_REF`** (Dockerfile) to the reviewed upstream commit that
   contains `/readyz`, then `python scripts/gen_garmin_tools.py` (CLAUDE.md
   hard constraint).
3. **Swap the gate:** `LoginGate` polls the worker's `/readyz` instead of
   reading log lines — `failed` → `WorkerCredentialsRejected` (re-auth 401,
   event `worker-login-rejected`), still `pending` at the
   `worker_startup_timeout` budget → `WorkerStartError`
   (`worker-login-timeout`). Keep event names and reasons unchanged (stable
   log schema). Drop the log-line matching and update `docs/architecture.md`
   + the CLAUDE.md "A healthy worker is not yet a signed-in worker" invariant.
   Extend `tests/conftest.py::fake_worker` with a `/readyz` knob.
4. **Smoke test** per CLAUDE.md release gate: Garmin sign-in (incl. MFA) and a
   worker spawned from stale tokens must surface the re-auth 401.

## Already shipped: bump `e8554bc..cfc5d79` (2026-10-05, separate PR)

- New tools (148 → 153 on the landing page): `get_energy_balance`, `get_stats_range`,
  `get_nutrition_summary_between_dates`, `get_recovery_time_remaining`,
  `create_run_interval_workout`.
- Fixes: nightly HRV and period averages, nutrition calorie/macro goals and
  settings targetDate, null sleep-phase crash, progress-summary calorie units,
  training-effect 403 fallback, `get_goals`, workout rest-step skip.
- `structured_output` off for string tools — removes duplicated
  `structuredContent` payloads (roughly halves response size).
- Error messages from `_GarminProxy` reworded ("Garmin authentication failed:
  … Re-run 'garmin-mcp-auth'…"). The login-gate lines we parse are
  **unchanged**, and no dependency changes in `pyproject.toml`.

## Comments

- 2026-10-05: everything up to `cfc5d79` shipped in its own bump (listed
  above), ahead of #381 — the next bump only needs to reach the commit that
  applies /readyz.
- 2026-10-12: upstream #380 landed as `8e809d8`. Pin bumped to tip-of-main
  `e79df6f` (contains it). Gate swapped onto `/readyz` polling. Upstream
  reports only `ready`|`pending`|`failed` — no MFA / password_reset / blocked
  on the worker path (those stay on the gateway form-login). Unreachable
  `/readyz` → timeout (`worker-login-timeout`), not stderr fallback. Stderr
  pump kept for diagnostics. (The worker's `garminconnect==0.3.2` pin no
  longer touches the gateway: since #46 the worker has its own venv.)
