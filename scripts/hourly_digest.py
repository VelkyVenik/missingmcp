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
  * heartbeat: a one-line "healthy" post, only on the HEARTBEAT_HOUR run.
  * otherwise: silent.
Re-auth signals (worker-start-failed / *-forward-auth-stale) are the expected
self-heal path and are NOT counted as anomalies.

Env:
  RAILWAY_API_TOKEN       account/workspace token (Bearer)         [required]
  RAILWAY_SERVICE_ID      gateway service uuid                     [required]
  RAILWAY_ENVIRONMENT_ID  production environment uuid              [required]
  SLACK_WEBHOOK_URL       incoming webhook            [required unless --dry-run]
  GATEWAY_URL             liveness-probe target (default https://missingmcp.com)
  HEARTBEAT_HOUR          local hour for the daily healthy heartbeat (default 8)
  REPORT_TZ               tz for HEARTBEAT_HOUR (default Europe/Prague)
  ANOMALY_MIN             min 5xx/error rows to escalate <!here> (default 3)

Usage: python scripts/hourly_digest.py [--dry-run] [--window-min 60]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
# Re-auth self-heal events: logged at error level but NOT anomalies (ticket 03).
SELF_HEAL_EVENTS = {"worker-start-failed", "local-forward-auth-stale",
                    "remote-forward-auth-stale"}


# --- Railway GraphQL I/O ---------------------------------------------------

def railway_graphql(token: str, query: str, variables: dict) -> dict:
    resp = httpx.post(RAILWAY_API, headers={"Authorization": f"Bearer {token}"},
                      json={"query": query, "variables": variables}, timeout=30.0)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"railway graphql errors: {payload['errors']}")
    return payload["data"]


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
    attrs = {a["key"]: a["value"] for a in (entry.get("attributes") or [])}
    level = (entry.get("severity") or attrs.get("level") or "").lower()
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
    is_heartbeat = datetime.now(tz).hour == heartbeat_hour
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
