#!/usr/bin/env python3
"""Hourly health digest → Slack.

Reads the gateway's last ~60 min of Railway logs via the Railway GraphQL API,
summarizes them, does a liveness probe, and posts to Slack — but QUIETLY: it
stays silent on healthy hours except one daily heartbeat, and escalates with a
Slack `<!here>` only on a real anomaly or a failed probe. Runs standalone in
GitHub Actions (httpx + stdlib only — does NOT import the missingmcp package).

Verdict (decided in .scratch/slack-hourly-digest ticket 03):
  * loud  (<!here>): >= ANOMALY_MIN 5xx/error rows, OR any `critical`, OR the
                     liveness probe failed.
  * minor (post, no ping): 1..ANOMALY_MIN-1 5xx/error rows.
  * heartbeat: a one-line "healthy" post, from the first successful run at/after
               HEARTBEAT_HOUR each day (GitHub cron is best-effort — a later run
               catches up when the scheduled hour was skipped; see heartbeat_due).
  * otherwise: silent.
Re-auth signals (worker-start-failed / *-forward-auth-stale) are the expected
self-heal path and are NOT counted as anomalies; worker-log traceback
continuation lines are folded into their preceding ERROR row.

Env:
  RAILWAY_API_TOKEN       account/workspace token (Bearer)         [required]
  RAILWAY_SERVICE_ID      gateway service uuid                     [required]
  RAILWAY_ENVIRONMENT_ID  production environment uuid              [required]
  SLACK_WEBHOOK_URL       incoming webhook            [required unless --dry-run]
  GATEWAY_URL             liveness-probe target (default https://missingmcp.com)
  HEARTBEAT_HOUR          local hour for the daily healthy heartbeat (default 8)
  REPORT_TZ               tz for HEARTBEAT_HOUR (default Europe/Prague)
  ANOMALY_MIN             min 5xx/error rows to escalate <!here> (default 3)
  GITHUB_TOKEN            Actions token for heartbeat catch-up (ambient in CI;
                          without it the heartbeat falls back to exact-hour match)

Usage: python scripts/hourly_digest.py [--dry-run] [--window-min 60]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
GITHUB_API = "https://api.github.com"
# Re-auth self-heal events: logged at error level but NOT anomalies (ticket 03).
SELF_HEAL_EVENTS = {"worker-start-failed", "local-forward-auth-stale",
                    "remote-forward-auth-stale"}
# workers.py elevates every worker stdout line matching ERROR|CRITICAL|Traceback|
# Exception — so one failed worker API call arrives as SEVERAL error rows (the
# ERROR head line plus its traceback decoration). Only head lines count as
# anomalies; the rest are continuations of the same failure.
_WORKER_ERR_HEAD = re.compile(r"\b(ERROR|CRITICAL)\b")


# --- Railway GraphQL I/O ---------------------------------------------------

def railway_graphql(token: str, query: str, variables: dict) -> dict:
    """POST a GraphQL query, supporting either Railway token type. A **project**
    token authenticates with the `Project-Access-Token` header; an **account/
    workspace** token uses `Authorization: Bearer`. Railway rejects the wrong
    header with a `Not Authorized` GraphQL error (HTTP 200), so try the
    project-token header first and fall back to Bearer on an auth error — this
    way `RAILWAY_API_TOKEN` can hold either type. (A project token is the
    narrower, preferred scope for CI: it only reaches this one project.)"""
    errors = None
    for headers in ({"Project-Access-Token": token},
                    {"Authorization": f"Bearer {token}"}):
        resp = httpx.post(RAILWAY_API, headers=headers,
                          json={"query": query, "variables": variables}, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("errors"):
            return payload["data"]
        errors = payload["errors"]
        if not any("Not Authorized" in str(e.get("message", "")) for e in errors):
            break   # a real query error — don't bother trying the other header
    raise RuntimeError(f"railway graphql errors: {errors}")


def resolve_deployment_id(token: str, service_id: str, environment_id: str) -> str:
    data = railway_graphql(token,
        "query($sid:String!,$eid:String!){ serviceInstance(serviceId:$sid,"
        "environmentId:$eid){ latestDeployment { id status } } }",
        {"sid": service_id, "eid": environment_id})
    dep = (data.get("serviceInstance") or {}).get("latestDeployment")
    if not dep or not dep.get("id"):
        raise RuntimeError("no latestDeployment for the given service/environment")
    return dep["id"]


def fetch_logs(token: str, deployment_id: str, start_iso: str, end_iso: str,
               limit: int = 5000) -> list[dict]:
    data = railway_graphql(token,
        "query($did:String!,$s:DateTime,$e:DateTime,$lim:Int){ "
        "deploymentLogs(deploymentId:$did,startDate:$s,endDate:$e,limit:$lim){ "
        "timestamp severity message attributes{key value} } }",
        {"did": deployment_id, "s": start_iso, "e": end_iso, "lim": limit})
    return data.get("deploymentLogs") or []


# --- pure log processing (unit-tested) -------------------------------------

def parse_row(entry: dict) -> dict:
    """Normalize a Railway Log entry into {level, event, account, status}. Reads
    the structured `severity` + `attributes[]`; falls back to JSON-parsing
    `message` whenever our `event` isn't present in the attributes (our log.py
    emits no top-level `message` key, so Railway's promotion into attributes[]
    is unconfirmed — ticket 01 watch-out; and Railway may inject its own
    platform attributes, so `attrs` being non-empty does NOT mean our fields
    are there)."""
    # Railway returns each attribute `value` JSON-encoded — a string field comes
    # back WITH quotes (event -> '"mcp-request"'), a number bare (status -> "200").
    # Decode so `event`/`account` are clean and status is an int; fall back to the
    # raw value when it isn't valid JSON.
    def _dec(v):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    attrs = {a["key"]: _dec(a["value"]) for a in (entry.get("attributes") or [])}
    level = str(entry.get("severity") or attrs.get("level") or "").lower()
    event = attrs.get("event")
    if not event:
        try:
            j = json.loads(entry.get("message") or "")
            if isinstance(j, dict):
                attrs = {k: str(v) for k, v in j.items()}
                level = (level or str(j.get("level", ""))).lower()
                event = j.get("event")
        except (ValueError, TypeError):
            pass
    status = attrs.get("status")
    try:
        status = int(status) if status is not None else None
    except (ValueError, TypeError):
        status = None
    # normalize Railway's abbreviated severities
    if level in ("err", "error"):
        level = "error"
    elif level in ("crit", "critical", "fatal"):
        level = "critical"
    elif level in ("warn", "warning"):
        level = "warn"
    if (event == "worker-log" and level in ("error", "critical")
            and not _WORKER_ERR_HEAD.search(str(attrs.get("line") or ""))):
        level = "info"   # traceback continuation, part of the preceding ERROR row
    return {"level": level, "event": event,
            "account": attrs.get("account"), "status": status}


def summarize(rows: list[dict]) -> dict:
    parsed = [parse_row(r) for r in rows]
    events = Counter(p["event"] for p in parsed if p["event"])
    statuses = Counter(p["status"] for p in parsed if p["status"] is not None)
    http_5xx = sum(n for s, n in statuses.items() if 500 <= s < 600)
    # error/critical rows that are NOT the expected self-heal path
    err_rows = sum(1 for p in parsed
                   if p["level"] in ("error", "critical")
                   and p["event"] not in SELF_HEAL_EVENTS)
    critical = sum(1 for p in parsed if p["level"] == "critical")
    reauth = sum(events.get(e, 0) for e in SELF_HEAL_EVENTS)
    requests = events.get("mcp-request", 0)
    accounts = len({p["account"] for p in parsed if p["account"]})
    # count each row at most once: a row is a "problem" if it's a 5xx OR an
    # unexpected (non-self-heal) error — never both, so no double-counting.
    problems = sum(
        1 for p in parsed
        if (p["status"] is not None and 500 <= p["status"] < 600)
        or (p["level"] in ("error", "critical") and p["event"] not in SELF_HEAL_EVENTS)
    )
    return {
        "rows": len(parsed), "requests": requests, "accounts": accounts,
        "http_5xx": http_5xx, "err_rows": err_rows, "critical": critical,
        "reauth": reauth, "statuses": dict(statuses),
        "problems": problems,
    }


def verdict(summary: dict, probe_ok: bool, is_heartbeat: bool,
            anomaly_min: int) -> dict:
    loud = (summary["problems"] >= anomaly_min or summary["critical"] > 0
            or not probe_ok)
    minor = (not loud) and summary["problems"] > 0
    return {"should_post": loud or minor or is_heartbeat,
            "loud": loud, "minor": minor, "heartbeat": is_heartbeat and not (loud or minor)}


def render(summary: dict, probe_ok: bool, v: dict, window_min: int,
           gateway_url: str) -> str:
    s = summary
    detail = (f"{s['requests']} requests · {s['accounts']} accounts · "
              f"statuses {s['statuses'] or '{}'} · {s['problems']} problem(s) "
              f"(5xx {s['http_5xx']}, errors {s['err_rows']}) · "
              f"re-auth {s['reauth']} · last {window_min}m")
    if not probe_ok:
        return f":red_circle: *MissingMCP gateway DOWN* — liveness probe to {gateway_url} failed. <!here>\n{detail}"
    if v["loud"]:
        return f":large_orange_circle: *MissingMCP — anomaly (last {window_min}m)* <!here>\n{detail}"
    if v["minor"]:
        return f":large_yellow_circle: MissingMCP — {s['problems']} problem(s) in the last {window_min}m (below alert threshold)\n{detail}"
    return f":large_green_circle: MissingMCP healthy — {detail}"


def heartbeat_due(now_local: datetime, heartbeat_hour: int,
                  prior_success_local: list[datetime] | None) -> bool:
    """True when this run should post the daily heartbeat. GitHub's cron is
    best-effort — runs get delayed or dropped outright — so exact hour-equality
    routinely misses the day's heartbeat. Instead the heartbeat belongs to the
    FIRST successful run at/after HEARTBEAT_HOUR local time each day; a later
    run catches up when the scheduled hour was skipped. With no visibility into
    prior runs (local invocation, API failure) fall back to hour-equality."""
    if prior_success_local is None:
        return now_local.hour == heartbeat_hour
    if now_local.hour < heartbeat_hour:
        return False
    return not any(t.date() == now_local.date() and t.hour >= heartbeat_hour
                   for t in prior_success_local)


def github_prior_successes(tz: ZoneInfo) -> list[datetime] | None:
    """Created-times (converted to tz) of this workflow's recent successful runs,
    via the GitHub API (the in-progress current run never matches status=success).
    None when not running in Actions or on any API failure."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")   # owner/repo/.github/workflows/<file>@ref
    wf = ref.split("@")[0].rsplit("/", 1)[-1] if ref else "hourly-digest.yml"
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        r = httpx.get(
            f"{GITHUB_API}/repos/{repo}/actions/workflows/{wf}/runs",
            params={"status": "success", "created": f">={since}", "per_page": 100},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=15.0)
        r.raise_for_status()
        runs = r.json().get("workflow_runs") or []
        return [datetime.strptime(x["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc).astimezone(tz) for x in runs]
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None


def probe(url: str) -> bool:
    """True if the gateway answers (any HTTP status < 500)."""
    try:
        r = httpx.get(url, timeout=15.0, follow_redirects=True)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def post_slack(webhook_url: str, text: str) -> None:
    r = httpx.post(webhook_url, json={"text": text}, timeout=15.0)
    if r.status_code != 200:
        raise RuntimeError(f"slack post rejected: HTTP {r.status_code}")


def _need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        sys.exit(f"missing required env var: {name}")
    return v


def main():
    p = argparse.ArgumentParser(description="Hourly gateway health digest → Slack.")
    p.add_argument("--dry-run", action="store_true", help="print, don't post")
    p.add_argument("--window-min", type=int, default=60, help="log window in minutes")
    args = p.parse_args()

    token = _need("RAILWAY_API_TOKEN")
    service_id = _need("RAILWAY_SERVICE_ID")
    environment_id = _need("RAILWAY_ENVIRONMENT_ID")
    gateway_url = os.environ.get("GATEWAY_URL", "https://missingmcp.com")
    anomaly_min = int(os.environ.get("ANOMALY_MIN", "3"))
    heartbeat_hour = int(os.environ.get("HEARTBEAT_HOUR", "8"))
    tz = ZoneInfo(os.environ.get("REPORT_TZ", "Europe/Prague"))

    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(minutes=args.window_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    deployment_id = resolve_deployment_id(token, service_id, environment_id)
    rows = fetch_logs(token, deployment_id, start_iso, end_iso)
    summary = summarize(rows)
    probe_ok = probe(gateway_url)
    now_local = datetime.now(tz)
    prior = github_prior_successes(tz)
    is_heartbeat = heartbeat_due(now_local, heartbeat_hour, prior)
    print("[heartbeat] due=%s, run visibility %s" % (
        is_heartbeat,
        "none — exact-hour fallback" if prior is None else f"ok ({len(prior)} recent)"))
    if is_heartbeat and now_local.hour != heartbeat_hour:
        print(f"[heartbeat] catch-up — no successful run landed in hour {heartbeat_hour} today")
    v = verdict(summary, probe_ok, is_heartbeat, anomaly_min)
    text = render(summary, probe_ok, v, args.window_min, gateway_url)

    print(text)
    print(f"[verdict] {v}")
    if not v["should_post"]:
        print("[silent] healthy hour, not the heartbeat — nothing posted.")
        return
    if args.dry_run:
        print("[dry-run] not posting.")
        return
    post_slack(_need("SLACK_WEBHOOK_URL"), text)
    print("[posted to Slack]")


if __name__ == "__main__":
    main()
