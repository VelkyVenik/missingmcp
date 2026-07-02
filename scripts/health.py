#!/usr/bin/env python3
"""Health check from the Garmin MCP Gateway logs.

Parses the gateway's structured JSON log events (and spots Garmin 429s in the
garminconnect debug lines) and prints a one-glance health report: how many logins
completed vs failed, whether workers came up healthy, MCP traffic, rate-limits,
restarts, and any error-level events. Green if nothing's wrong, otherwise it lists
what to look at.

Usage:
  python scripts/health.py                         # reads GATEWAY_LOG_FILE / ./.localdata / /data
  python scripts/health.py --file /data/gateway.log
  docker compose logs gateway | python scripts/health.py     # from stdin
  docker compose logs --since 24h gateway | python /app/scripts/health.py
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter


def get_stream(file_arg):
    if file_arg:
        return open(file_arg, encoding="utf-8", errors="replace")
    if not sys.stdin.isatty():          # piped in (docker compose logs | ...)
        return sys.stdin
    path = os.environ.get("GATEWAY_LOG_FILE")
    if not path:
        for cand in ("./.localdata/gateway.log", "/data/gateway.log"):
            if os.path.exists(cand):
                path = cand
                break
    if not path or not os.path.exists(path):
        sys.exit("No log source. Pass --file, set GATEWAY_LOG_FILE, or pipe logs in "
                 "(docker compose logs gateway | python scripts/health.py).")
    return open(path, encoding="utf-8", errors="replace")


def main():
    p = argparse.ArgumentParser(description="Health report from the gateway logs.")
    p.add_argument("--file", help="log file to read (default: auto / stdin)")
    p.add_argument("--errors", type=int, default=10, help="how many recent errors to list")
    args = p.parse_args()

    ev = Counter()                # event -> count
    login_results = Counter()     # needs_mfa / ok
    errors = []                   # (ts, event, detail)
    r429 = 0
    last_stats = None
    lines = 0

    for raw in get_stream(args.file):
        line = raw.strip()
        # docker compose prefixes lines with "gateway-1  | "; strip it
        if "|" in line[:24] and "{" in line:
            line = line[line.index("{"):]
        if not line:
            continue
        lines += 1
        if line.startswith("{"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            e = r.get("event", "?")
            ev[e] += 1
            if r.get("level") == "error":
                detail = " ".join(f"{k}={v}" for k, v in r.items()
                                  if k not in ("ts", "level", "event", "traceback"))
                errors.append((r.get("ts", ""), e, detail))
            if e == "login-start-result":
                login_results[r.get("status", "?")] += 1
            elif e == "stats":
                last_stats = r
        elif "429" in line or "rate limited" in line.lower():
            r429 += 1

    worker_fail = ev["worker-unhealthy"] + ev["worker-spawn-failed"] + ev["worker-start-failed"]
    forward_err = ev["mcp-forward-error"]
    login_fail = (ev["login-start-failed"] + ev["mfa-resume-failed"]
                  + ev["mfa-verify-failed"] + ev["login-verify-failed"])
    issues = worker_fail + forward_err

    print(f"\nGarmin MCP Gateway — health  ({lines} log lines)\n")
    verdict = "✅ healthy" if issues == 0 else f"⚠️  {issues} issue(s) worth a look"
    print(f"Verdict: {verdict}\n")

    print("Runtime")
    print(f"  gateway starts (restarts) : {ev['gateway-started']}")
    if last_stats:
        s = last_stats
        print(f"  latest stats              : accounts={s.get('accounts')} "
              f"tokens={s.get('tokens')} people={s.get('people_with_token')} "
              f"workers={s.get('active_workers')}")

    print("\nConnections (OAuth)")
    print(f"  login attempts            : {ev['login-start']}")
    print(f"  → needs MFA / clean       : {login_results['needs_mfa']} / {login_results['ok']}")
    print(f"  → completed (token issued): {ev['token-issued']}")
    print(f"  login/MFA failures        : {login_fail}   (usually wrong password/code)")
    if ev["authorize-csrf-invalid"] or ev["mfa-session-missing"]:
        print(f"  csrf-invalid / mfa-expired: {ev['authorize-csrf-invalid']} / {ev['mfa-session-missing']}")

    print("\nWorkers")
    print(f"  spawned / started OK      : {ev['worker-spawn']} / {ev['worker-started']}")
    print(f"  unhealthy / spawn-failed  : {ev['worker-unhealthy']} / {ev['worker-spawn-failed'] + ev['worker-start-failed']}")
    print(f"  reaped / evicted          : {ev['worker-reaped']} / {ev['worker-evicted']}")
    if ev["worker-cap-all-busy"]:
        print(f"  cap hit (all busy)        : {ev['worker-cap-all-busy']}")

    print("\nMCP traffic")
    print(f"  requests                  : {ev['mcp-request']}")
    print(f"  auth rejected (401)       : {ev['mcp-auth-rejected']}   (clients without a valid token — usually normal)")
    print(f"  forward errors (502/504)  : {forward_err}")

    print("\nGarmin API")
    print(f"  rate-limited (429) hits   : {r429}   (slows logins; not a gateway bug)")

    if errors:
        print(f"\nErrors (level=error), last {args.errors} of {len(errors)}")
        for ts, e, detail in errors[-args.errors:]:
            print(f"  [{ts}] {e}  {detail}")
    print()


if __name__ == "__main__":
    main()
