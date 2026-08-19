#!/usr/bin/env python3
"""Daily triage → Slack: analyze the last 24 h of production problems and post
what happened → what it means → PROPOSED ACTION, per error class — or a one-line
"all quiet" on healthy days. Replaces the hourly digest's error counting as the
operator's main channel (design: .scratch/reliability/issues/08, operator-grilled
2026-08-19); the hourly workflow survives as a hard-signal pager only.

Runs standalone in GitHub Actions (httpx + stdlib only — does NOT import the
missingmcp package). Data comes from PostHog (logs + $mcp events) via the Query
API (HogQL); classification of KNOWN signatures is deterministic in this script,
and only the non-routine remainder goes to the Claude API for analysis and
proposed actions. Egress rule: aggregates and masked samples only — account
e-mails are masked to 3 chars before anything leaves this script, and MCP
bodies/credentials never appear in logs to begin with.

Verdict:
  * healthy  — no actionable classes: post ONE line (the daily heartbeat /
               dead-man switch), skip the Claude call entirely.
  * analysis — actionable classes present: Claude gets the aggregates and
               writes the triage; on Claude failure/refusal the deterministic
               aggregate table posts instead (degraded, never silent).

Env:
  POSTHOG_QUERY_KEY    personal API key with query-read scope (phx_...)  [required]
  POSTHOG_PROJECT_ID   PostHog project id             (default 227772)
  POSTHOG_HOST         PostHog host                   (default https://eu.posthog.com)
  ANTHROPIC_API_KEY    Claude API key                 [required unless --dry-run]
  SLACK_WEBHOOK_URL    incoming webhook               [required unless --dry-run]
  TRIAGE_MODEL         Claude model id                (default claude-opus-5)

Usage: python scripts/daily_triage.py [--dry-run] [--window-hours 24]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time

import httpx

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WORKER_ERR_HEAD = re.compile(r"\b(ERROR|CRITICAL)\b")

# Known-signature table (reliability tickets 01/06/09): class name, routine flag.
# Routine classes never trigger the analysis on their own volume alone — they are
# reported in the aggregates for context, and Claude sees them only when a
# non-routine class opened the analysis. Order matters: first match wins.
ROUTINE_CLASSES = {"credential-expiry", "auth-flow-noise"}
BURST_THRESHOLDS = {"garmin-upstream": 150}   # routine below this many rows/day

_SELF_HEAL_EVENTS = {"worker-forward-auth-stale", "local-forward-auth-stale",
                     "remote-forward-auth-stale", "worker-exited-early"}
_AUTH_NOISE_EVENTS = {"authorize-csrf-invalid", "authorize-client-id-not-dcr",
                      "login-start-failed", "mfa-resume-failed"}


# --- PostHog Query API (HogQL) ----------------------------------------------

def posthog_query(host: str, project_id: str, key: str, sql: str) -> list:
    """One HogQL query via POST /api/environments/:id/query. Returns `results`
    (list of rows, each a list of columns)."""
    resp = httpx.post(
        f"{host}/api/environments/{project_id}/query",
        headers={"Authorization": f"Bearer {key}"},
        json={"query": {"kind": "HogQLQuery", "query": sql}},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json().get("results") or []


def collect(host: str, project_id: str, key: str, window_hours: int) -> dict:
    """The day's raw material: error/fatal log rows, stream-teardown warn counts,
    and traffic figures. Row bodies are our own structured-JSON log lines."""
    w = int(window_hours)
    error_rows = posthog_query(host, project_id, key, f"""
        SELECT body, severity_text FROM logs
        WHERE timestamp >= now() - INTERVAL {w} HOUR
          AND severity_text IN ('error', 'fatal')
        ORDER BY timestamp DESC LIMIT 2000""")
    teardown = posthog_query(host, project_id, key, f"""
        SELECT countIf(body NOT LIKE '%"tool": null%') AS post_side, count() AS total
        FROM logs
        WHERE timestamp >= now() - INTERVAL {w} HOUR
          AND severity_text = 'warn'
          AND body LIKE '%mcp-stream-interrupted%'""")
    traffic = posthog_query(host, project_id, key, f"""
        SELECT count() AS calls, count(DISTINCT distinct_id) AS accounts
        FROM events
        WHERE event = '$mcp_tool_call'
          AND timestamp >= now() - INTERVAL {w} HOUR""")
    post_side, teardown_total = (teardown[0] if teardown else (0, 0))
    calls, accounts = (traffic[0] if traffic else (0, 0))
    return {"error_rows": [r[0] for r in error_rows],
            "teardown_total": int(teardown_total), "teardown_post_side": int(post_side),
            "tool_calls": int(calls), "active_accounts": int(accounts)}


# --- deterministic classification (unit-tested) -----------------------------

def mask(text: str) -> str:
    """Mask every e-mail to its first 3 chars — nothing identifying leaves."""
    return _EMAIL.sub(lambda m: m.group(0)[:3] + "***", text)


def _classify_one(row: dict) -> "str | None":
    """Signature table. Returns the class name, or None for rows that are
    continuations of a previous row (worker-log traceback decoration)."""
    event = row.get("event", "")
    line = str(row.get("line", ""))
    if event == "worker-log":
        if not _WORKER_ERR_HEAD.search(line):
            return None                                   # traceback continuation
        if "OAuth tokens not found" in line:
            return "credential-expiry"
        if ("API call failed" in line or "Connection error" in line
                or "ASGI callable returned" in line):
            return "garmin-upstream"
        return "worker-log-other"
    if event in _SELF_HEAL_EVENTS:
        return "credential-expiry"
    if event in _AUTH_NOISE_EVENTS:
        return "auth-flow-noise"
    if event in ("worker-start-failed", "worker-spawn-failed"):
        return "worker-fault"
    if event == "mcp-forward-error":
        return "forward-error"
    if event == "stdlib-log":
        msg = str(row.get("message", ""))
        if "Login failed" in msg:
            return "garmin-upstream"
        return "gateway-fault"
    return "unclassified"


def classify(error_rows: list) -> dict:
    """Aggregate raw error bodies into {class: {count, samples}}. Samples are
    masked and truncated; at most 3 per class."""
    classes: dict = {}
    for body in error_rows:
        try:
            row = json.loads(body)
        except (ValueError, TypeError):
            row = {"event": "unparseable", "message": str(body)[:200]}
        cls = _classify_one(row)
        if cls is None:
            continue
        bucket = classes.setdefault(cls, {"count": 0, "samples": []})
        bucket["count"] += 1
        if len(bucket["samples"]) < 3:
            bucket["samples"].append(mask(str(body))[:300])
    return classes


def actionable_classes(classes: dict) -> list:
    """The classes that justify waking the analyst: everything non-routine,
    plus routine classes past their burst threshold."""
    out = []
    for name, data in classes.items():
        if name in ROUTINE_CLASSES:
            continue
        threshold = BURST_THRESHOLDS.get(name)
        if threshold is not None and data["count"] < threshold:
            continue
        out.append(name)
    return sorted(out)


# --- Claude analysis ---------------------------------------------------------

TRIAGE_PROMPT = """You are the daily production-triage analyst for missingmcp.com, \
a small OAuth gateway that hosts per-user MCP connectors (Garmin via per-user \
worker subprocesses, WHOOP in-process). You receive one JSON object: the last \
24 hours of pre-aggregated error classes (with up to 3 masked sample log lines \
each), stream-teardown counts, and traffic figures.

Write the operator's daily Slack message. Rules:
- Per problem class, in order of how actionable it is: WHAT HAPPENED (one line, \
with numbers), WHAT IT MEANS (one line), PROPOSED ACTION (one concrete step — \
a command, a ticket to open, a thing to watch; never "investigate further" alone).
- Known context you may rely on: credential-expiry and auth-flow-noise are \
routine self-heal classes; garmin-upstream is Garmin-side flakiness (actionable \
only on sustained bursts); a surge of POST-side stream teardowns would be \
user-facing; forward-error (ConnectError on worker forwards) is tracked as \
reliability ticket 12.
- Plain Slack text (no markdown headers, *bold* is fine), under 40 lines.
- Never invent numbers; the input is the only source. E-mails are pre-masked.
- If nothing truly needs the operator today, say so in one line and stop."""


def claude_analyze(api_key: str, model: str, aggregates: dict) -> "str | None":
    """One Messages API call; returns the analysis text, or None when the
    analysis is unavailable (refusal, exhausted retries) — the caller degrades
    to the deterministic summary, never silence."""
    body = {
        "model": model,
        "max_tokens": 16000,   # hard cap on thinking + text; the message itself is short
        "system": TRIAGE_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(aggregates, sort_keys=True)}],
    }
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    for attempt in range(3):
        try:
            resp = httpx.post(ANTHROPIC_API, headers=headers, json=body, timeout=300.0)
        except httpx.HTTPError as e:
            print(f"[claude] network error: {type(e).__name__}", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (429, 500, 529):
            wait = int(resp.headers.get("retry-after", 2 ** attempt))
            print(f"[claude] HTTP {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            print(f"[claude] HTTP {resp.status_code}: {resp.text[:300]}")
            return None
        data = resp.json()
        if data.get("stop_reason") == "refusal":
            print("[claude] refusal — posting deterministic summary instead")
            return None
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        return text or None
    return None


# --- rendering + the notify seam ---------------------------------------------

def render(aggregates: dict, analysis: "str | None") -> str:
    """The Slack message. Healthy day → one line; analysis day → Claude's text
    (or the deterministic degraded table when analysis is None)."""
    a = aggregates
    traffic = (f"{a['tool_calls']} tool calls · {a['active_accounts']} accounts · "
               f"teardowns {a['teardown_total']} ({a['teardown_post_side']} POST-side)")
    if not a["actionable"]:
        return f":large_green_circle: Daily triage: all quiet — {traffic}. Nothing needs you today."
    if analysis:
        return f":clipboard: *Daily triage — action needed*\n{analysis}\n_{traffic}_"
    lines = [f":clipboard: *Daily triage — action needed* (analysis unavailable, raw aggregates)"]
    for name in sorted(a["classes"], key=lambda n: -a["classes"][n]["count"]):
        data = a["classes"][name]
        flag = " ←" if name in a["actionable"] else ""
        lines.append(f"• {name}: {data['count']}{flag}")
    lines.append(f"_{traffic}_")
    return "\n".join(lines)


def notify(webhook_url: str, text: str) -> None:
    """THE single posting seam — the operator plans to move off Slack
    (reliability ticket 13); keep every channel touch inside this function so
    the swap is a config change, not a rewrite."""
    r = httpx.post(webhook_url, json={"text": text}, timeout=15.0)
    if r.status_code != 200:
        raise RuntimeError(f"slack post rejected: HTTP {r.status_code}")


def _need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        sys.exit(f"missing required env var: {name}")
    return v


def main():
    p = argparse.ArgumentParser(description="Daily production triage → Slack.")
    p.add_argument("--dry-run", action="store_true", help="print, don't post")
    p.add_argument("--window-hours", type=int, default=24)
    args = p.parse_args()

    host = os.environ.get("POSTHOG_HOST", "https://eu.posthog.com")
    project_id = os.environ.get("POSTHOG_PROJECT_ID", "227772")
    ph_key = _need("POSTHOG_QUERY_KEY")
    model = os.environ.get("TRIAGE_MODEL", DEFAULT_MODEL)

    raw = collect(host, project_id, ph_key, args.window_hours)
    classes = classify(raw["error_rows"])
    aggregates = {
        "window_hours": args.window_hours,
        "classes": classes,
        "actionable": actionable_classes(classes),
        "teardown_total": raw["teardown_total"],
        "teardown_post_side": raw["teardown_post_side"],
        "tool_calls": raw["tool_calls"],
        "active_accounts": raw["active_accounts"],
    }
    print(f"[aggregates] {json.dumps({k: v for k, v in aggregates.items() if k != 'classes'})}")
    for name, data in sorted(classes.items()):
        print(f"[class] {name}: {data['count']}")

    analysis = None
    if aggregates["actionable"]:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            analysis = claude_analyze(api_key, model, aggregates)
        elif not args.dry_run:
            _need("ANTHROPIC_API_KEY")

    text = render(aggregates, analysis)
    print(text)
    if args.dry_run:
        print("[dry-run] not posting.")
        return
    notify(_need("SLACK_WEBHOOK_URL"), text)
    print("[posted to Slack]")


if __name__ == "__main__":
    main()
