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
