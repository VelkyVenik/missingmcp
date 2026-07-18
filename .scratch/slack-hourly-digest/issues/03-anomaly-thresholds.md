# 03 — Anomaly thresholds for the loud/@here path

Type: grilling
Status: resolved

## Question

The hybrid cadence posts quietly when healthy and escalates (`@here`) on anomaly.
Define exactly what "anomaly" is for a 60-minute window, so the summarizer can emit
a healthy/anomaly verdict deterministically:

- Any `mcp-response` status 5xx → anomaly? (how many?)
- Any `error`/`critical` level rows → anomaly? (any, or a count?)
- Re-auth signals (`worker-start-failed` / `*-forward-auth-stale`): expected
  low-level now (that's the self-heal path) — anomaly only above N/hour, or per
  distinct account?
- **Zero traffic** for a whole hour → anomaly (gateway may be down) or silence?
- Timing-of-day: quieter overnight — does zero-traffic overnight still alert?
- What does `@here` vs a plain post look like, and should healthy hours post at all
  or only every Nth (to cut 24 msgs/day)?

Output feeds the summarizer's verdict logic (ticket 04).

## Answer

Resolved 2026-07-18 (grilling with the operator).

- **Loud/@here trigger:** the 60-min window flips to expanded + `@here` when it
  contains **≥3 `mcp-response` 5xx OR ≥3 error-level rows, OR any `critical` row**
  (N=3, tunable). A single/occasional 5xx or error below N is **mentioned in the
  post body but does NOT `@here`**. Re-auth signals (`worker-start-failed` /
  `*-forward-auth-stale`) are the expected self-heal path — NOT anomalies on their
  own.
- **Liveness / zero-traffic:** zero traffic alone **never alerts** (few users;
  absence ≠ failure). Instead the job does **one HTTP GET to the gateway** (public
  URL — landing or a well-known endpoint); a **probe failure → `@here` "gateway
  down"** regardless of time of day. This is the real "is it down?" signal.
- **Healthy cadence:** **silent when healthy.** The cron still runs **hourly** (for
  prompt anomaly/probe detection) but only POSTS when (a) anomaly, (b) probe
  failure, or (c) it's the **once-daily heartbeat** run (~09:00 Europe/Prague),
  which posts a one-line "still healthy — N req, statuses, 0 errors" summary. The
  daily heartbeat doubles as proof the job itself is alive (so silence stays
  unambiguous).
- Formatting (plain text vs Block Kit) stays a build-time detail (map fog).
