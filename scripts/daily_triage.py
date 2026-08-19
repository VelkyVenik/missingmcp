#!/usr/bin/env python3
"""Daily triage → Slack: analyze the last 24 h of production problems and post
what happened → what it means → PROPOSED ACTION, per error class — or a one-line
"all quiet" on healthy days. Replaces the hourly digest's error counting as the
operator's main channel (design: .scratch/reliability/issues/08, operator-grilled
2026-08-19); the hourly workflow survives as a hard-signal pager only.

Runs standalone in GitHub Actions (httpx + stdlib only — does NOT import the
missingmcp package; it does reuse scripts/hourly_digest.py's Railway client).
Data comes from the gateway's Railway logs (the same stream PostHog receives —
the PostHog Query API turned out to be plan-gated, so the free, proven Railway
path won); classification of KNOWN signatures is deterministic in this script,
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
  RAILWAY_API_TOKEN       project or account token (as in hourly_digest)  [required]
  RAILWAY_SERVICE_ID      gateway service uuid                            [required]
  RAILWAY_ENVIRONMENT_ID  production environment uuid                     [required]
  CLAUDE_CODE_OAUTH_TOKEN Claude Code subscription token (`claude setup-token`)
                          — analysis runs via headless `claude -p` on the
                          operator's subscription (preferred; no API credits)
  ANTHROPIC_API_KEY       Claude API key — fallback backend when the
                          subscription token is absent
  SLACK_WEBHOOK_URL       incoming webhook               [required unless --dry-run]
  TRIAGE_MODEL            Claude model id, API backend only (default claude-opus-5)

Usage: python scripts/daily_triage.py [--dry-run] [--window-hours 24]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
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


# --- Railway logs (via hourly_digest's client) --------------------------------
# hourly_digest.py lives next to this script; python puts the script dir on
# sys.path, so the import works both in CI and under pytest.
import hourly_digest as hd  # noqa: E402

_LOGS_QUERY = (
    "query($did:String!,$s:DateTime,$e:DateTime,$lim:Int,$f:String){ "
    "deploymentLogs(deploymentId:$did,startDate:$s,endDate:$e,limit:$lim,filter:$f){ "
    "timestamp severity message attributes{key value} } }")

# Railway logs are PER DEPLOYMENT — a 24 h window against only the latest
# deployment silently loses everything before the day's last deploy (pushes to
# main deploy automatically, so that's most days). Cover the window by querying
# every deployment that overlapped it; REMOVED deployments' logs stay
# retrievable, verified live 2026-08-19.
_DEPLOYMENTS_QUERY = (
    "query($in:DeploymentListInput!,$n:Int){ deployments(input:$in, first:$n)"
    "{ edges { node { id createdAt } } } }")


def list_deployments(token: str, service_id: str, environment_id: str,
                     n: int = 15) -> list:
    data = hd.railway_graphql(token, _DEPLOYMENTS_QUERY, {
        "in": {"serviceId": service_id, "environmentId": environment_id}, "n": n})
    return [e["node"] for e in (data.get("deployments") or {}).get("edges") or []]


def pick_deployments(deployments: list, start_iso: str) -> list:
    """Deployment ids overlapping [start, now]: every deployment created inside
    the window, plus the newest one created before it (live at window start).
    Timestamps compare lexicographically after truncation to seconds."""
    def _key(d):
        return d["createdAt"][:19]
    inside, before = [], []
    for d in sorted(deployments, key=_key, reverse=True):
        (inside if _key(d) >= start_iso[:19] else before).append(d)
    picked = inside + before[:1]
    return [d["id"] for d in picked]


def row_dict(entry: dict) -> dict:
    """A Railway log entry → our structured-log dict (all fields, JSON-decoded
    attribute values; message-JSON fallback like hourly_digest.parse_row)."""
    def _dec(v):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    row = {a["key"]: _dec(a["value"]) for a in (entry.get("attributes") or [])}
    if "event" not in row:
        try:
            j = json.loads(entry.get("message") or "")
            if isinstance(j, dict):
                row = j
        except (ValueError, TypeError):
            row = {"event": "unparseable", "message": str(entry.get("message"))[:200]}
    return row


def _fetch(token: str, deployment_id: str, start_iso: str, end_iso: str,
           severity_filter: str) -> list:
    data = hd.railway_graphql(token, _LOGS_QUERY, {
        "did": deployment_id, "s": start_iso, "e": end_iso,
        "lim": 5000, "f": severity_filter})
    return data.get("deploymentLogs") or []


def collect(token: str, service_id: str, environment_id: str,
            window_hours: int) -> dict:
    """The day's raw material: error/fatal rows (classified downstream) and the
    stream-teardown warn counts. Traffic/usage figures deliberately stay out —
    the gateway's own 08:00 user-stats report covers those."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    deployment_ids = pick_deployments(
        list_deployments(token, service_id, environment_id), start)
    print(f"[collect] {len(deployment_ids)} deployment(s) overlap the window")
    error_rows, warn_rows = [], []
    for did in deployment_ids:
        error_rows += [row_dict(e) for e in _fetch(token, did, start, end,
                                                   "@level:error")]
        warn_rows += [row_dict(e) for e in _fetch(token, did, start, end,
                                                  "@level:warn")]
    teardowns = [r for r in warn_rows if r.get("event") == "mcp-stream-interrupted"]
    post_side = sum(1 for r in teardowns if r.get("tool") is not None)
    return {"error_rows": error_rows,
            "teardown_total": len(teardowns), "teardown_post_side": post_side}


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
    """Aggregate parsed error rows into {class: {count, samples}}. Samples are
    masked and truncated; at most 3 per class."""
    classes: dict = {}
    for row in error_rows:
        if not isinstance(row, dict):
            row = {"event": "unparseable", "message": str(row)[:200]}
        cls = _classify_one(row)
        if cls is None:
            continue
        bucket = classes.setdefault(cls, {"count": 0, "samples": []})
        bucket["count"] += 1
        if len(bucket["samples"]) < 3:
            bucket["samples"].append(mask(json.dumps(row, sort_keys=True))[:300])
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

TRIAGE_PROMPT = """Jsi ranní provozní hlásič pro missingmcp.com. Tvůj čtenář je \
provozovatel služby — čte to u kávy a chce vědět jediné: děje se něco, co bolí \
uživatele nebo službu, a mám dnes něco udělat? Není to vývojář ponořený v kódu; \
výpis výjimek ho nezajímá.

Než cokoli napíšeš, zorientuj se (běžíš v checkoutu repa, máš read-only \
přístup): přečti si .scratch/reliability/map.md (hlavně "Decisions so far") a \
projdi otevřené tickety v .scratch/reliability/issues/ — co už je vyřešené, \
známé nebo se sleduje. Vstupní JSON v tomto promptu jsou agregované chyby za \
posledních 24 h.

Pravidla zprávy (ČESKY, maximálně 8 řádků, žádný úvod ani závěr):
- Mluv o DOPADU, ne o technologii: "cca 100 pokusům o připojení Garminu dnes \
selhal první pokus (klient to zopakuje a projde)" je dobře; "ConnectError \
traceback z httpx" je špatně. Technický název třídy dej jen jednou do závorky.
- Co už je známé a má ticket: JEDNA řádka — "známé, sleduje ticket X, dnes N \
výskytů, trend". Žádná nová akce, žádné vysvětlování.
- Novou akci navrhuj jen u věcí bez ticketu — formuluj ji jako jednu větu, \
kterou provozovatel může říct svému agentovi ("řekni Claudovi, ať…").
- Rutinní samoopravné třídy vůbec nezmiňuj. Když není co řešit: jedna řádka.
- Čísla ber jen ze vstupu, nic nedomýšlej. E-maily jsou maskované."""


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


def claude_analyze_subscription(aggregates: dict) -> "str | None":
    """The subscription backend: headless Claude Code (`claude -p`) authenticated
    by CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`) — the operator's Claude
    subscription pays for the analysis instead of API credits. Same contract as
    claude_analyze: text, or None to degrade."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--output-format", "text",
             # Read-only repo access: the analyst grounds itself in the live
             # tracker (.scratch/reliability) before writing — no Bash/Write.
             "--allowedTools", "Read,Grep,Glob",
             "--append-system-prompt", TRIAGE_PROMPT,
             json.dumps(aggregates, sort_keys=True)],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[claude-code] failed: {type(e).__name__}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"[claude-code] rc={proc.returncode}: {proc.stderr[:300]}", file=sys.stderr)
        return None
    return proc.stdout.strip() or None


def analyze(aggregates: dict) -> "str | None":
    """Backend dispatch: subscription token first (free under the operator's
    plan), API key as fallback, None (degraded) when neither is configured."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return claude_analyze_subscription(aggregates)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        return claude_analyze(api_key, os.environ.get("TRIAGE_MODEL", DEFAULT_MODEL),
                              aggregates)
    print("[analyze] no CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY — degraded")
    return None


# --- rendering + the notify seam ---------------------------------------------

def render(aggregates: dict, analysis: "str | None") -> str:
    """The Slack message. Healthy day → one line; analysis day → Claude's text
    (or the deterministic degraded table when analysis is None)."""
    a = aggregates
    traffic = (f"{a['error_row_count']} error řádků · teardowny {a['teardown_total']} "
               f"({a['teardown_post_side']} POST-side) · posledních {a['window_hours']} h")
    if not a["actionable"]:
        return f":large_green_circle: Denní triage: klid — {traffic}. Dnes není co řešit."
    if analysis:
        return f":clipboard: *Denní triage — je co řešit*\n{analysis}\n_{traffic}_"
    lines = [":clipboard: *Denní triage — je co řešit* (analýza nedostupná, surové agregáty)"]
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

    token = _need("RAILWAY_API_TOKEN")
    service_id = _need("RAILWAY_SERVICE_ID")
    environment_id = _need("RAILWAY_ENVIRONMENT_ID")
    raw = collect(token, service_id, environment_id, args.window_hours)
    classes = classify(raw["error_rows"])
    aggregates = {
        "window_hours": args.window_hours,
        "classes": classes,
        "actionable": actionable_classes(classes),
        "teardown_total": raw["teardown_total"],
        "teardown_post_side": raw["teardown_post_side"],
        "error_row_count": len(raw["error_rows"]),
    }
    print(f"[aggregates] {json.dumps({k: v for k, v in aggregates.items() if k != 'classes'})}")
    for name, data in sorted(classes.items()):
        print(f"[class] {name}: {data['count']}")

    analysis = None
    if aggregates["actionable"]:
        analysis = analyze(aggregates)

    text = render(aggregates, analysis)
    print(text)
    if args.dry_run:
        print("[dry-run] not posting.")
        return
    notify(_need("SLACK_WEBHOOK_URL"), text)
    print("[posted to Slack]")


if __name__ == "__main__":
    main()
