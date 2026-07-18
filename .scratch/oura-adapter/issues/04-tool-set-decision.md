# 04 — Tool set decision

Type: grilling
Status: resolved
Blocked by: 01

## Question

Which Oura endpoints become tools in the `TOOLS` table, under what names, with what
pagination/date-range parameters? Curated core (whoop shipped 8 read-only tools) vs.
everything the API offers? Include: how membership-gated data should behave for accounts
without a membership (error copy vs. empty results), and whether `personal_info` /
ring-configuration data belongs in the set. Per the Auth model decision (ticket 03),
every tool's scope must be named: only `daily`-scoped tools are guaranteed; the rest are
degradable, so each tool declares which scope it needs. Output feeds directly into the
spec's tools section and later `scripts/gen_oura_tools.py`.

## Answer

Resolved 2026-07-17 (grilling with the operator).

- **Coverage: full 1:1** — every non-deprecated `/v2/usercollection/*` list endpoint
  becomes a tool (~18): daily_activity, daily_readiness, daily_sleep, daily_spo2,
  daily_stress, daily_resilience, daily_cardiovascular_age, sleep, sleep_time, heartrate,
  workout, session, enhanced_tag, rest_mode_period, ring_configuration,
  ring_battery_level, vO2_max, personal_info. Deprecated `tag` is excluded
  (enhanced_tag covers it).
- **No by-id variants** — list endpoints return complete documents, so single-document
  routes add no value Claude can't get by filtering a list. Deliberate deviation from the
  whoop precedent (which shipped get_sleep/get_workout by UUID), justified by set size
  (~18 vs ~30).
- **Naming**: `get_` + endpoint name (get_daily_sleep, get_enhanced_tags, …); exact
  names and descriptions land in the spec's TOOLS table.
- **Params**: `start_date`/`end_date` + `next_token` everywhere; heartrate uses
  `start_datetime`/`end_datetime` (per the research asset).
- **Scopes**: each TOOLS entry declares its scope (mechanical mapping from the API docs;
  the `spo2` vs `spo2Daily` string is verified empirically during implementation).
  Missing scope → the degradable-scope error from the Auth model decision.
- **Membership 403**: a friendly in-result MCP error — "Oura returns data only with an
  active membership — renew it in the Oura app". NOT mapped to SessionExpired/502 (the
  tokens are fine; "reconnect" would be a lying diagnosis) and never empty data (silent
  lie). The connection stays alive and recovers by itself once the membership is renewed.
