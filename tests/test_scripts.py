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
