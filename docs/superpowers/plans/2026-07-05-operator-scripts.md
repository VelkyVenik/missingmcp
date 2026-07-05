# Operator Scripts (Plan B, Part 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the DB-reading operator scripts (`scripts/status.py`, `revoke.py`, `usage.py`) up to the adapter-aware, MissingMCP-branded target shape from spec `docs/superpowers/specs/2026-07-05-garmin-finish-and-home-design.md` Part 3, and **delete** the log-reading scripts (`monitor.py`, `health.py`) — the deployment is on Railway, which owns log viewing/analysis. Plan A only kept the scripts mechanically working after the schema rename; this is the UX pass.

**Architecture:** The scripts stay standalone stdlib-only CLIs that read the SQLite DB directly (no `garmin_gateway` imports — they must run via bare `python3` inside the container or over `railway ssh`). `status.py` becomes the single "see everything" overview (accounts → devices with token-hash prefixes → usage summary); `revoke.py` gets adapter-scoped `--account [<adapter>:]<key>`, `--device <hash-prefix>`, and `--purge`; `usage.py` groups by `(adapter, account_key)`; `monitor.py` and `health.py` are removed and README/CLAUDE.md updated to match. New `tests/test_scripts.py` exercises the DB-reading scripts against a seeded temp DB by importing each script with `importlib` and calling its `main()`.

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `argparse`, `json`); pytest via `uv run --extra dev pytest`.

## Global Constraints

- **Scripts are standalone**: stdlib-only, never `import garmin_gateway` — they are copied to `/app/scripts` and run with bare `python3` (Docker exec / `railway ssh --service gateway`). Tests *may* import `garmin_gateway.store` to seed the DB (dev env has the package).
- **Do not touch `src/garmin_gateway/`** — this plan changes scripts, tests, README, and CLAUDE.md only. Log event names and the `status`/`reason` values remain a stable schema (operators query them in Railway logs) — nothing in this plan renames gateway events.
- **Naming:** user-facing headers say `MissingMCP gateway — …`; docstrings say "the MCP gateway". Package rename `garmin_gateway`→`missingmcp` is a spec non-goal — out of scope.
- **`account_key`** is the normalized lowercased login email; bare `--account <key>` defaults to adapter `garmin` (constant `DEFAULT_ADAPTER = "garmin"`). Adapter-qualified form is `<adapter>:<key>`, split on the **first** `:`.
- **`resolve_db()` order is load-bearing** and unchanged: `$DB_PATH` → `$DATA_DIR/gateway.db` → `/data/gateway.db` → `./.localdata/gateway.db`. It stays duplicated per script (established pattern — scripts can't share a module and stay standalone); `parse_account` is likewise duplicated in `revoke.py` and `usage.py`, byte-identical.
- **Two documented deviations from the spec text** (approved intent, sharper semantics):
  1. `--purge` deletes the `tool_usage` rows as well as the `accounts` row — otherwise a purged account leaves ghost metrics in `status.py`/`usage.py`.
  2. `--token-hash <full sha256>` is **replaced** by `--device <prefix>` (a full hash is a valid prefix). One flag, README updated.
- Tests: `uv run --extra dev pytest -q` must be green at the end of every task. TDD per task: failing test → implement → pass → commit.
- Work on a branch (`feat/operator-scripts`), never directly on `main`.

## Latent bug being fixed (context for the reviewer)

Today `revoke.py --account me@x.cz` runs `DELETE FROM access_tokens WHERE account_key=?` **without an adapter filter** — with a second adapter, revoking a user's Garmin access would silently log them out of every other connector too. Task 2's adapter-scoped delete fixes this.

---

### Task 1: Test harness + `status.py` unified overview

**Files:**
- Create: `tests/test_scripts.py`
- Modify: `scripts/status.py` (full rewrite of the output section; `resolve_db` and the workers.json block survive verbatim)

**Interfaces:**
- Produces (used by Tasks 2 and 3, which append to the same test file):
  - `load_script(name: str) -> module` — imports `scripts/<name>.py` via importlib.
  - `run_script(name: str, argv: list[str], capsys, monkeypatch) -> str` — runs a script's `main()` with patched `sys.argv`, returns captured stdout.
  - fixture `seeded_db(tmp_path) -> str` — path to a DB with 2 accounts (same email on adapters `garmin` + `rohlik`), 3 tokens (hash prefixes `aa11…`, `bb22…` garmin; `cc33…` rohlik), 1 OAuth client `Claude`, and usage rows (garmin: 2× `get_activities` + 1× `get_sleep_data` = calls 3 / 2 tools; rohlik: 1× `get_cart`).
- Produces: `scripts/status.py` output containing per-account lines `"<adapter>:<key> … devices: N …"`, a `usage: calls: N across M tool(s)` line, and per-device lines starting with the 8-char token-hash prefix (consumed by `revoke.py --device`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scripts.py`:

```python
"""Operator scripts (scripts/*.py) against a seeded adapter-aware DB.

The scripts are standalone (not a package), so they are loaded via importlib
and driven through their main() with a patched argv.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys

import pytest

from garmin_gateway import store

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
SECRET = "s" * 32


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_script(name: str, argv: list[str], capsys, monkeypatch) -> str:
    mod = load_script(name)
    monkeypatch.setattr(sys, "argv", [f"{name}.py"] + argv)
    mod.main()
    return capsys.readouterr().out


@pytest.fixture
def seeded_db(tmp_path):
    """Two adapters sharing one email — the trap every query must not fall into."""
    path = str(tmp_path / "gateway.db")
    conn = store.init_db(path)
    store.upsert_account(conn, "garmin", "alice@example.com", '{"tokens": "x"}', SECRET)
    store.upsert_account(conn, "rohlik", "alice@example.com", '{"cookies": "y"}', SECRET)
    store.create_client(conn, "client-1", "secret-hash",
                        ["https://claude.ai/api/mcp/auth_callback"], "Claude", "garmin")
    store.create_access_token(conn, "aa11" + "0" * 60, "garmin", "alice@example.com", "client-1")
    store.create_access_token(conn, "bb22" + "0" * 60, "garmin", "alice@example.com", "client-1")
    store.create_access_token(conn, "cc33" + "0" * 60, "rohlik", "alice@example.com", "client-1")
    store.record_usage(conn, "garmin", "alice@example.com", "get_activities")
    store.record_usage(conn, "garmin", "alice@example.com", "get_activities")
    store.record_usage(conn, "garmin", "alice@example.com", "get_sleep_data")
    store.record_usage(conn, "rohlik", "alice@example.com", "get_cart")
    conn.close()
    return path


# --- status.py -------------------------------------------------------------

def test_status_overview_is_adapter_aware(seeded_db, capsys, monkeypatch):
    out = run_script("status", ["--db", seeded_db], capsys, monkeypatch)
    assert "MissingMCP" in out
    assert "Garmin MCP Gateway" not in out
    assert "garmin:alice@example.com" in out
    assert "rohlik:alice@example.com" in out


def test_status_shows_devices_and_usage(seeded_db, capsys, monkeypatch):
    out = run_script("status", ["--db", seeded_db], capsys, monkeypatch)
    assert "aa110000" in out                       # device line: token-hash prefix
    assert "calls: 3 across 2 tool(s)" in out      # garmin usage summary
    assert "calls: 1 across 1 tool(s)" in out      # rohlik usage summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_scripts.py -v`
Expected: FAIL — `test_status_overview_is_adapter_aware` on `assert "MissingMCP" in out` (header still says "Garmin MCP Gateway"), `test_status_shows_devices_and_usage` on the `aa110000` assert.

- [ ] **Step 3: Rewrite `scripts/status.py`**

Replace the whole file with:

```python
#!/usr/bin/env python3
"""Status / stats snapshot for the MCP gateway (reads the DB read-only).

One overview: every connected account (adapter:key) with its devices —
token-hash prefixes you can pass to revoke.py --device — plus a tool-usage
summary, the registered OAuth clients, and the running workers. Safe to run
while the gateway is live (opens the SQLite DB read-only).

Usage:
  python scripts/status.py                 # uses ./.localdata/gateway.db
  python scripts/status.py --db /data/gateway.db
  railway ssh --service gateway "python3 /app/scripts/status.py"
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys


def resolve_db() -> str:
    """Find the SQLite DB without flags: explicit env first, then the usual
    container (/data) and local-dev (./.localdata) locations."""
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def main():
    p = argparse.ArgumentParser(description="MCP gateway status snapshot.")
    p.add_argument("--db", default=None,
                   help="SQLite DB path (default: $DB_PATH, $DATA_DIR/gateway.db, "
                        "/data/gateway.db, or ./.localdata/gateway.db)")
    args = p.parse_args()
    db_path = args.db or resolve_db()

    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731

    accounts = one("SELECT COUNT(*) FROM accounts")
    tokens = one("SELECT COUNT(*) FROM access_tokens")
    people = one("SELECT COUNT(DISTINCT adapter || ':' || account_key) FROM access_tokens")
    clients = one("SELECT COUNT(*) FROM oauth_clients")
    pending = one("SELECT COUNT(*) FROM oauth_codes")

    print(f"\nMissingMCP gateway — status  ({db_path})\n")
    print("Summary")
    print(f"  People with a token : {people}")
    print(f"  Access tokens       : {tokens}   (devices/clients connected)")
    print(f"  Accounts            : {accounts}")
    print(f"  OAuth clients       : {clients}   (registered apps)")
    print(f"  Pending auth codes  : {pending}")

    client_names = {c["client_id"]: (c["client_name"] or "(unnamed)")
                    for c in db.execute("SELECT client_id, client_name FROM oauth_clients")}
    tokens_by_acct: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for t in db.execute(
        "SELECT adapter, account_key, token_hash, client_id, created_at, last_used "
        "FROM access_tokens ORDER BY created_at"
    ):
        tokens_by_acct.setdefault((t["adapter"], t["account_key"]), []).append(t)
    usage_by_acct = {
        (u["adapter"], u["account_key"]): u
        for u in db.execute(
            "SELECT adapter, account_key, SUM(calls) AS calls, COUNT(*) AS tools, "
            "MAX(last_used) AS last FROM tool_usage GROUP BY adapter, account_key"
        )
    }

    rows = db.execute(
        "SELECT adapter, account_key, created_at FROM accounts ORDER BY created_at"
    ).fetchall()
    if rows:
        print("\nAccounts")
        for a in rows:
            k = (a["adapter"], a["account_key"])
            devices = tokens_by_acct.pop(k, [])
            last = max((t["last_used"] or "" for t in devices), default="") or "—"
            print(f"  {a['adapter']}:{a['account_key']:<32} devices: {len(devices):<3} "
                  f"connected: {a['created_at']}  last used: {last}")
            u = usage_by_acct.get(k)
            if u:
                print(f"    usage: calls: {u['calls']} across {u['tools']} tool(s), "
                      f"last {u['last']}")
            for t in devices:
                print(f"    {t['token_hash'][:8]}…  "
                      f"{client_names.get(t['client_id'], '?'):<24} "
                      f"created: {t['created_at']}  last used: {t['last_used'] or '—'}")
    if tokens_by_acct:  # tokens whose account row is gone (e.g. after --purge)
        print("\nTokens without a stored account (revoke these)")
        for (adapter, key), devices in tokens_by_acct.items():
            for t in devices:
                print(f"  {t['token_hash'][:8]}…  {adapter}:{key}")

    crows = db.execute(
        """
        SELECT c.adapter AS adapter, c.client_name AS name, c.redirect_uris AS redirect,
               COUNT(t.token_hash) AS tokens,
               GROUP_CONCAT(DISTINCT t.account_key) AS accounts
        FROM oauth_clients c
        LEFT JOIN access_tokens t ON t.client_id = c.client_id
        GROUP BY c.client_id ORDER BY c.created_at
        """
    ).fetchall()
    if crows:
        print("\nOAuth clients (registered)")
        for r in crows:
            name = r["name"] or "(unnamed)"
            accts = r["accounts"] or "—  (never completed OAuth)"
            print(f"  [{r['adapter']}] {name:<28} account: {accts:<28} "
                  f"tokens: {r['tokens']:<3} {r['redirect']}")

    # Active runners live in the gateway's memory; it persists a snapshot to
    # workers.json (next to the DB) so we can show them here.
    wpath = os.path.join(os.path.dirname(os.path.abspath(db_path)), "workers.json")
    print("\nActive runners (per-account MCP workers)")
    if os.path.exists(wpath):
        try:
            snap = json.load(open(wpath, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snap = None
        if snap and snap.get("workers"):
            for w in snap["workers"]:
                state = "alive" if w.get("alive") else "DEAD"
                print(f"  {w['key']:<32} port {w['port']}  pid {w['pid']}  "
                      f"{state}  idle {w['idle_seconds']}s")
            print(f"  (snapshot at {snap.get('updated', '?')})")
        elif snap is not None:
            print(f"  none running   (snapshot at {snap.get('updated', '?')})")
        else:
            print("  (workers.json unreadable)")
    else:
        print("  (no snapshot — gateway not running yet, or a different DATA_DIR)")
    print()


if __name__ == "__main__":
    main()
```

(Only the docstring, the header/labels, the Accounts section, and the OAuth-clients `[adapter]` tag changed; `resolve_db`, the Summary queries, and the workers.json block are today's code verbatim.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_scripts.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite + commit**

Run: `uv run --extra dev pytest -q` — expected: all green.

```bash
git add tests/test_scripts.py scripts/status.py
git commit -m "feat(scripts): status.py — adapter-aware overview with devices + usage summary"
```

---

### Task 2: `revoke.py` — adapter-scoped `--account`, `--device` prefix, `--purge`

**Files:**
- Modify: `scripts/revoke.py` (full rewrite below)
- Test: `tests/test_scripts.py` (append)

**Interfaces:**
- Consumes from Task 1: `run_script`, `load_script`, `seeded_db`, `SECRET` in `tests/test_scripts.py`; `garmin_gateway.store` for extra seeding.
- Produces CLI: `revoke.py --list | --account [<adapter>:]<key> [--purge] | --device <hash-prefix>`; `parse_account(value: str) -> tuple[str, str]` (module-level, also duplicated into `usage.py` by Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scripts.py`:

```python
# --- revoke.py -------------------------------------------------------------

def _tokens(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT token_hash, adapter FROM access_tokens ORDER BY token_hash").fetchall()
    conn.close()
    return rows


def test_revoke_account_scopes_to_adapter(seeded_db, capsys, monkeypatch):
    out = run_script("revoke", ["--db", seeded_db, "--account", "garmin:alice@example.com"],
                     capsys, monkeypatch)
    assert "Revoked 2 token(s) for garmin:alice@example.com" in out
    assert [r[1] for r in _tokens(seeded_db)] == ["rohlik"]   # rohlik token untouched
    conn = sqlite3.connect(seeded_db)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2  # rows intact
    conn.close()


def test_revoke_bare_key_defaults_to_garmin(seeded_db, capsys, monkeypatch):
    run_script("revoke", ["--db", seeded_db, "--account", "Alice@Example.com"],
               capsys, monkeypatch)                            # also: normalized lowercase
    assert [r[1] for r in _tokens(seeded_db)] == ["rohlik"]


def test_revoke_device_by_prefix(seeded_db, capsys, monkeypatch):
    out = run_script("revoke", ["--db", seeded_db, "--device", "aa110000"],
                     capsys, monkeypatch)
    assert "Revoked device" in out and "garmin:alice@example.com" in out
    hashes = [r[0] for r in _tokens(seeded_db)]
    assert len(hashes) == 2 and not any(h.startswith("aa11") for h in hashes)


def test_revoke_device_ambiguous_prefix_refuses(seeded_db, capsys, monkeypatch):
    conn = store.init_db(seeded_db)
    store.create_access_token(conn, "dd44eeff" + "a" * 56, "garmin", "alice@example.com", "client-1")
    store.create_access_token(conn, "dd44eeff" + "b" * 56, "garmin", "alice@example.com", "client-1")
    conn.close()
    with pytest.raises(SystemExit):
        run_script("revoke", ["--db", seeded_db, "--device", "dd44eeff"], capsys, monkeypatch)
    assert len(_tokens(seeded_db)) == 5                        # nothing deleted


def test_revoke_device_short_prefix_refuses(seeded_db, capsys, monkeypatch):
    with pytest.raises(SystemExit):
        run_script("revoke", ["--db", seeded_db, "--device", "aa11"], capsys, monkeypatch)
    assert len(_tokens(seeded_db)) == 3


def test_revoke_purge_removes_account_and_usage(seeded_db, capsys, monkeypatch):
    out = run_script("revoke",
                     ["--db", seeded_db, "--account", "garmin:alice@example.com", "--purge"],
                     capsys, monkeypatch)
    assert "Purged" in out
    conn = sqlite3.connect(seeded_db)
    assert conn.execute("SELECT adapter FROM accounts").fetchall() == [("rohlik",)]
    assert conn.execute("SELECT DISTINCT adapter FROM tool_usage").fetchall() == [("rohlik",)]
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_scripts.py -v`
Expected: Task 1 tests still PASS; the six new revoke tests FAIL (`--device`/`--purge` are unknown arguments → argparse `SystemExit`, making e.g. `test_revoke_device_by_prefix` fail on the missing "Revoked device" output; `test_revoke_account_scopes_to_adapter` fails because the rohlik token is deleted too — the latent bug).

- [ ] **Step 3: Rewrite `scripts/revoke.py`**

Replace the whole file with:

```python
#!/usr/bin/env python3
"""Revoke MCP gateway access tokens — a kill-switch for a leaked/lost token.

Usage:
  python scripts/revoke.py --list                      # accounts + token counts
  python scripts/revoke.py --account me@x.cz           # ALL garmin tokens for an account
  python scripts/revoke.py --account rohlik:me@x.cz    # adapter-scoped
  python scripts/revoke.py --account me@x.cz --purge   # + delete stored account & usage
  python scripts/revoke.py --device ab12cd34           # one device, by token-hash prefix
                                                       #   (prefixes shown by status.py)

Revoking tokens only logs devices out; the account's stored service tokens are
left intact, so the user simply re-authenticates in Claude. --purge also deletes
the encrypted account row and its usage metrics (full off-boarding). The running
gateway re-checks the DB on every request, so revocation takes effect immediately.

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
In Docker:  docker compose exec gateway python /app/scripts/revoke.py --account <email>
On Railway: railway ssh --service gateway "python3 /app/scripts/revoke.py --account <email>"
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

DEFAULT_ADAPTER = "garmin"


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def parse_account(value: str) -> tuple[str, str]:
    """'rohlik:me@x.cz' -> ('rohlik', 'me@x.cz'); a bare key defaults to garmin.
    Keys are stored lowercased (oauth._finish), so normalize here too."""
    adapter, sep, key = value.partition(":")
    if not sep:
        return DEFAULT_ADAPTER, adapter.strip().lower()
    return adapter.strip().lower(), key.strip().lower()


def main():
    p = argparse.ArgumentParser(description="Revoke gateway access tokens.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--purge", action="store_true",
                   help="with --account: also delete the stored account row + usage metrics")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list accounts and token counts")
    g.add_argument("--account", metavar="[ADAPTER:]KEY",
                   help="revoke ALL tokens for this account (bare key = garmin)")
    g.add_argument("--device", metavar="HASH_PREFIX",
                   help="revoke one device by its token-hash prefix "
                        "(>=8 chars; a full hash works too)")
    args = p.parse_args()
    if args.purge and not args.account:
        p.error("--purge only makes sense with --account")

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if args.list:
        rows = conn.execute(
            "SELECT account_key AS key, adapter, COUNT(*) AS n, MAX(last_used) AS last "
            "FROM access_tokens GROUP BY adapter, account_key ORDER BY adapter, account_key"
        ).fetchall()
        if not rows:
            print("No access tokens.")
        for r in rows:
            print(f"  {r['adapter']}:{r['key']:<32} tokens: {r['n']:<3} "
                  f"last used: {r['last'] or '—'}")
        return

    if args.device:
        prefix = args.device.strip().lower()
        if len(prefix) < 8:
            sys.exit("Prefix too short — give at least 8 characters (status.py shows them).")
        rows = conn.execute(
            "SELECT token_hash, adapter, account_key FROM access_tokens "
            "WHERE token_hash LIKE ?", (prefix + "%",)
        ).fetchall()
        if not rows:
            print(f"No token matches {prefix}…")
            return
        if len(rows) > 1:
            for r in rows:
                print(f"  {r['token_hash'][:16]}…  {r['adapter']}:{r['account_key']}")
            sys.exit(f"Ambiguous prefix — {len(rows)} tokens match; give more characters.")
        conn.execute("DELETE FROM access_tokens WHERE token_hash=?",
                     (rows[0]["token_hash"],))
        conn.commit()
        print(f"Revoked device {prefix}… of "
              f"{rows[0]['adapter']}:{rows[0]['account_key']}.")
        return

    adapter, key = parse_account(args.account)
    cur = conn.execute(
        "DELETE FROM access_tokens WHERE adapter=? AND account_key=?", (adapter, key))
    msg = f"Revoked {cur.rowcount} token(s) for {adapter}:{key}."
    if args.purge:
        conn.execute("DELETE FROM tool_usage WHERE adapter=? AND account_key=?",
                     (adapter, key))
        purged = conn.execute(
            "DELETE FROM accounts WHERE adapter=? AND account_key=?", (adapter, key))
        msg += (" Purged the stored account + usage." if purged.rowcount
                else " No stored account row to purge.")
    else:
        msg += " They must reconnect in Claude."
    conn.commit()
    print(msg)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_scripts.py -v`
Expected: 8 passed.

- [ ] **Step 5: Full suite + commit**

Run: `uv run --extra dev pytest -q` — expected: all green.

```bash
git add tests/test_scripts.py scripts/revoke.py
git commit -m "feat(scripts): revoke.py — adapter-scoped --account, --device prefix, --purge"
```

---

### Task 3: `usage.py` — adapter-aware grouping and filters

**Files:**
- Modify: `scripts/usage.py` (full rewrite below)
- Test: `tests/test_scripts.py` (append)

**Interfaces:**
- Consumes from Task 1: `run_script`, `seeded_db`.
- Consumes from Task 2: the exact `DEFAULT_ADAPTER` + `parse_account` code (duplicated verbatim into this script — scripts stay standalone).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scripts.py`:

```python
# --- usage.py --------------------------------------------------------------

def test_usage_summary_groups_by_adapter(seeded_db, capsys, monkeypatch):
    out = run_script("usage", ["--db", seeded_db], capsys, monkeypatch)
    assert "garmin:alice@example.com" in out       # two lines, not one merged line
    assert "rohlik:alice@example.com" in out
    assert "MissingMCP" in out


def test_usage_account_filter_is_adapter_aware(seeded_db, capsys, monkeypatch):
    out = run_script("usage", ["--db", seeded_db, "--account", "rohlik:alice@example.com"],
                     capsys, monkeypatch)
    assert "get_cart" in out
    assert "get_activities" not in out


def test_usage_bare_account_defaults_to_garmin(seeded_db, capsys, monkeypatch):
    out = run_script("usage", ["--db", seeded_db, "--account", "alice@example.com"],
                     capsys, monkeypatch)
    assert "get_activities" in out
    assert "get_cart" not in out


def test_usage_tools_leaderboard_shows_adapter(seeded_db, capsys, monkeypatch):
    out = run_script("usage", ["--db", seeded_db, "--tools"], capsys, monkeypatch)
    assert "garmin:get_activities" in out
    assert "rohlik:get_cart" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_scripts.py -v`
Expected: earlier tests PASS; the four new ones FAIL (`test_usage_summary_groups_by_adapter` on the missing `garmin:`-prefixed line, `test_usage_account_filter_is_adapter_aware` because the un-scoped filter returns garmin rows too).

- [ ] **Step 3: Rewrite `scripts/usage.py`**

Replace the whole file with:

```python
#!/usr/bin/env python3
"""Per-account tool/method usage for the MCP gateway (reads the DB read-only).

Shows how many MCP calls each connected account made and which tools/methods
they used. Only names + counts are recorded — never request contents or any
service data. Counts include MCP protocol methods (initialize, tools/list, …)
that Claude calls automatically, alongside actual tool calls.

Usage:
  python scripts/usage.py                          # per-account summary + top tools
  python scripts/usage.py --tools                  # overall tool/method leaderboard
  python scripts/usage.py --account me@x           # one garmin account's breakdown
  python scripts/usage.py --account rohlik:me@x    # adapter-scoped

DB path resolves like status.py: $DB_PATH, $DATA_DIR/gateway.db, /data, ./.localdata.
In Docker:  docker compose exec gateway python /app/scripts/usage.py
On Railway: railway ssh --service gateway "python3 /app/scripts/usage.py"
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

DEFAULT_ADAPTER = "garmin"


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def parse_account(value: str) -> tuple[str, str]:
    """'rohlik:me@x.cz' -> ('rohlik', 'me@x.cz'); a bare key defaults to garmin.
    Keys are stored lowercased (oauth._finish), so normalize here too."""
    adapter, sep, key = value.partition(":")
    if not sep:
        return DEFAULT_ADAPTER, adapter.strip().lower()
    return adapter.strip().lower(), key.strip().lower()


def main():
    p = argparse.ArgumentParser(description="MCP gateway usage metrics.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--account", metavar="[ADAPTER:]KEY",
                   help="show per-tool breakdown for this account (bare key = garmin)")
    p.add_argument("--tools", action="store_true", help="overall tool/method leaderboard")
    p.add_argument("--limit", type=int, default=15, help="rows in leaderboards (default 15)")
    args = p.parse_args()

    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    if db.execute("SELECT COUNT(*) FROM tool_usage").fetchone()[0] == 0:
        print("No usage recorded yet.")
        return

    if args.account:
        adapter, key = parse_account(args.account)
        rows = db.execute(
            "SELECT tool, calls, last_used FROM tool_usage "
            "WHERE adapter=? AND account_key=? ORDER BY calls DESC",
            (adapter, key),
        ).fetchall()
        if not rows:
            print(f"No usage recorded for {adapter}:{key}.")
            return
        print(f"\nUsage for {adapter}:{key}\n")
        for r in rows:
            print(f"  {r['tool']:<28} {r['calls']:>5}   last: {r['last_used']}")
        print()
        return

    if args.tools:
        rows = db.execute(
            "SELECT adapter, tool, SUM(calls) AS n, COUNT(DISTINCT account_key) AS users "
            "FROM tool_usage GROUP BY adapter, tool ORDER BY n DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        print("\nTop tools / methods (all accounts)\n")
        for r in rows:
            label = f"{r['adapter']}:{r['tool']}"
            print(f"  {label:<36} {r['n']:>6}   by {r['users']} user(s)")
        print()
        return

    # default: per-account summary + top tools
    print(f"\nMissingMCP gateway — usage  ({db_path})\n")
    print("Per account")
    for r in db.execute(
        "SELECT adapter, account_key AS key, SUM(calls) AS calls, "
        "COUNT(DISTINCT tool) AS tools, MAX(last_used) AS last "
        "FROM tool_usage GROUP BY adapter, account_key ORDER BY calls DESC"
    ).fetchall():
        acct = f"{r['adapter']}:{r['key']}"
        print(f"  {acct:<38} calls: {r['calls']:<6} tools: {r['tools']:<4} last: {r['last']}")

    print("\nTop tools / methods")
    for r in db.execute(
        "SELECT adapter, tool, SUM(calls) AS n FROM tool_usage "
        "GROUP BY adapter, tool ORDER BY n DESC LIMIT ?",
        (args.limit,),
    ).fetchall():
        label = f"{r['adapter']}:{r['tool']}"
        print(f"  {label:<36} {r['n']:>6}")
    print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_scripts.py -v`
Expected: 12 passed.

- [ ] **Step 5: Full suite + commit**

Run: `uv run --extra dev pytest -q` — expected: all green.

```bash
git add tests/test_scripts.py scripts/usage.py
git commit -m "feat(scripts): usage.py — adapter-aware grouping and filters"
```

---

### Task 4: Delete `monitor.py` + `health.py`; update README & CLAUDE.md

Deployment is on Railway — `railway logs` is the live view and log analysis happens in the Railway dashboard, so the two log-reading scripts are dead weight (user decision 2026-07-06). No new tests; the gate is the full suite plus grep-clean docs.

**Files:**
- Delete: `scripts/monitor.py`, `scripts/health.py`
- Modify: `README.md` (Monitoring section; "Before you deploy" revoke bullet)
- Modify: `CLAUDE.md` (scripts list in "What this is"; `health.py` mention in the log-schema invariant)

**Interfaces:**
- Consumes: the CLI shapes produced by Tasks 1–3 (documented verbatim in README).

- [ ] **Step 1: Delete the scripts**

```bash
git rm scripts/monitor.py scripts/health.py
```

- [ ] **Step 2: Rewrite the README Monitoring section**

In `README.md`, replace the whole **Monitoring** section (from `## Monitoring` up to, not including, `## How it works`) with:

````markdown
## Monitoring

Three helper scripts read the gateway's DB (safe while the gateway is live):

```bash
python scripts/status.py          # snapshot: accounts, their devices (token
                                  #   prefixes), usage summary, running workers
python scripts/revoke.py --list                       # accounts + token counts
python scripts/revoke.py --account [<adapter>:]<key>  # kill-switch: revoke ALL the
                                                      #   account's tokens (bare key = garmin)
python scripts/revoke.py --account <key> --purge      # + delete stored account & usage
python scripts/revoke.py --device <hash-prefix>       # revoke ONE device (prefix from status.py)
python scripts/usage.py                               # per-account tool usage + leaderboard
python scripts/usage.py --account [<adapter>:]<key>   # one account's per-tool breakdown
```

**With Docker** the scripts are baked into the image at `/app/scripts`; run them
inside the container. `status.py` finds the DB under `/data` automatically:

```bash
docker compose exec gateway python /app/scripts/status.py
docker compose logs -f gateway              # live structured-JSON events
```

**On Railway** run them over `railway ssh`; logs live in the Railway dashboard
(`railway logs --service gateway` for a live tail):

```bash
railway ssh --service gateway "python3 /app/scripts/status.py"
railway ssh --service gateway "python3 /app/scripts/revoke.py --account <email>"
```

The gateway's own log is structured JSON (one event per line). Each per-user
worker's verbose output is kept out of it, in `DATA_DIR/users/<account>/worker.log`
(look there to debug a specific worker). The gateway also logs a `stats` event
(accounts / tokens / people-with-token / clients / active-workers) on startup and
whenever those counts change, and `status.py` lists the running workers.
````

- [ ] **Step 3: Update the "Before you deploy" revoke bullet**

In `README.md` → **Before you deploy**, change the revoking bullet's command to
`python scripts/revoke.py --account [<adapter>:]<email>` and append: "a single
device can be revoked with `--device <hash-prefix>` (prefixes are shown by
`status.py`)".

- [ ] **Step 4: Update CLAUDE.md**

Two edits:

1. In **What this is**, change

   > operational scripts (`status`, `monitor`, `revoke`, `usage`, `health`) live in `scripts/`

   to

   > operational scripts (`status`, `revoke`, `usage`) live in `scripts/`

2. In **Cross-cutting invariants**, change

   > Log event names and fields are a stable schema (`scripts/health.py` parses them) — refactors must not rename events or the `status`/`reason` values.

   to

   > Log event names and fields are a stable schema (operators query them in Railway logs) — refactors must not rename events or the `status`/`reason` values.

- [ ] **Step 5: Verify**

```bash
uv run --extra dev pytest -q                       # expected: all green
grep -rn "monitor.py\|health.py" README.md CLAUDE.md   # expected: no matches
ls scripts/                                        # expected: revoke.py status.py usage.py (+__pycache__)
```

(`docs/superpowers/` historical specs/plans still mention the deleted scripts — that's an accurate record of past decisions; leave them.)

- [ ] **Step 6: Commit**

```bash
git add -A scripts/ README.md CLAUDE.md
git commit -m "chore(scripts): drop monitor.py + health.py — Railway owns log viewing"
```

---

## Self-review notes

- **Spec coverage:** Part 3's status overview (Task 1), adapter-aware revoke with `--device`/`--purge` (Task 2), adapter-aware usage (Task 3 — same class of multi-adapter bug as revoke's), tests against a seeded DB (Tasks 1–3), Railway run instructions (docstrings + README in Tasks 1–4). Deleting `monitor.py`/`health.py` is a user decision from the 2026-07-06 plan review, superseding the original Part 3 reading.
- **Type consistency:** `parse_account` is byte-identical in `revoke.py` and `usage.py`; `seeded_db`/`run_script` signatures match across tasks; token prefixes `aa110000`/`dd44eeff` are consistent between fixture and asserts (fixture hashes are 64 chars: 4 + 60×`0`, ambiguity pair 8 + 56).
- **Deviations** (flagged in Global Constraints): `--purge` also clears `tool_usage`; `--token-hash` replaced by `--device`.
