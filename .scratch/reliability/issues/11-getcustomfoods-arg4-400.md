# 11 — garmin_mcp custom-foods listing 400s: page size 100 > Garmin's cap of 20

Type: upstream-bug (watch)
Status: needs-info

## What we saw

One occurrence in production, 2026-09-04T13:50 UTC (first day on the
`e8554bcd` pin), worker traceback:

```
API call failed for path '/nutrition-service/customFood': API Error 400 -
{'errors': ["'getCustomFoods.arg4' must be less than or equal to 20
(provided value: 100)"]}
garminconnect.exceptions.GarminConnectConnectionError: API Error 400 - ...
```

Garmin's `/nutrition-service/customFood` endpoint caps its page-size
parameter (`arg4`) at 20; `garmin_mcp`'s custom-foods path passes 100.
The nutrition/custom-foods code was reworked in the window we bumped
through (upstream #225 added `search_foods` + source routing for
`log_custom_food`), so this is most plausibly an upstream regression in
that rework — nothing in the gateway touches tool arguments.

## Decision (Václav, 2026-09-05)

Do **not** file the upstream issue yet — note it and keep watching. If it
recurs (or once someone confirms the hardcoded 100 in upstream source),
file it at github.com/Taxuspt/garmin_mcp with the traceback above.

## How to watch

Daily triage aggregates elevated `worker-log` lines; grep Railway logs for
`getCustomFoods.arg4` or `/nutrition-service/customFood`. Recurrence =
promote to ready-for-human (file upstream).
