"""scripts/backfill_account_connected.py — replays account_connected from the DB.

Telemetry goes through a stubbed SDK client (telemetry._client), same pattern as
test_telemetry.py / test_beers.py; the real PostHog SDK is never touched. The
script is standalone (not a package), so it's loaded via importlib.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timezone

import pytest

from garmin import store, telemetry

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "scripts_backfill_ac", SCRIPTS / "backfill_account_connected.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load()


class Recorder:
    def __init__(self):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None, timestamp=None, uuid=None):
        self.events.append({"event": event, "distinct_id": distinct_id,
                            "properties": properties, "timestamp": timestamp, "uuid": uuid})

    def shutdown(self):
        pass


def _insert(conn, adapter, key, created_at):
    conn.execute(
        "INSERT INTO accounts (adapter, account_key, blob_enc, created_at) VALUES (?,?,?,?)",
        (adapter, key, "x", created_at))
    conn.commit()


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def recorder(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(telemetry, "_client", r)
    monkeypatch.setattr(telemetry, "_api_key", "phc_test")
    return r


# --- loading + --before filter ----------------------------------------------

def test_load_accounts_before_filter(conn):
    _insert(conn, "garmin", "old@x.cz", "2026-06-01 10:00:00")
    _insert(conn, "whoop", "mid@x.cz", "2026-07-01 10:00:00")
    _insert(conn, "garmin", "new@x.cz", "2026-07-25 10:00:00")
    cutoff = datetime(2026, 7, 20, tzinfo=timezone.utc)
    keys = [a["account_key"] for a in bf.load_accounts(conn, before=cutoff)]
    assert keys == ["old@x.cz", "mid@x.cz"]              # oldest-first, 07-25 excluded
    assert len(bf.load_accounts(conn, before=None)) == 3  # no filter → all


# --- dry-run vs apply --------------------------------------------------------

def test_dry_run_emits_nothing(conn, recorder):
    _insert(conn, "garmin", "a@x.cz", "2026-06-01 10:00:00")
    accounts = bf.backfill(conn, apply=False)
    assert len(accounts) == 1
    assert recorder.events == []                          # dry-run sends nothing


def test_apply_emits_account_connected(conn, recorder):
    _insert(conn, "garmin", "a@x.cz", "2026-06-01 10:00:00")
    bf.backfill(conn, apply=True)
    ev = recorder.events[0]
    assert ev["event"] == "account_connected"
    assert ev["distinct_id"] == "a@x.cz"
    assert ev["properties"] == {"adapter": "garmin", "connect_status": "new", "backfill": True}
    assert ev["timestamp"] == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)  # backdated
    assert ev["uuid"] == bf.event_uuid("garmin", "a@x.cz")                      # deterministic


def test_event_uuid_deterministic_and_distinct():
    assert bf.event_uuid("garmin", "a@x.cz") == bf.event_uuid("garmin", "a@x.cz")
    assert bf.event_uuid("garmin", "a@x.cz") != bf.event_uuid("whoop", "a@x.cz")
    assert bf.event_uuid("garmin", "a@x.cz") != bf.event_uuid("garmin", "b@x.cz")


# --- main() ------------------------------------------------------------------

def test_main_dry_run_prints_and_emits_nothing(tmp_path, capsys, monkeypatch, recorder):
    path = str(tmp_path / "gateway.db")
    c = store.init_db(path)
    _insert(c, "garmin", "a@x.cz", "2026-06-01 10:00:00")
    _insert(c, "whoop", "b@x.cz", "2026-07-01 10:00:00")
    c.close()
    monkeypatch.setattr(sys, "argv", ["backfill.py", "--db", path])
    bf.main()
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "a@x.cz" in out and "b@x.cz" in out
    assert "total: 2 account(s)" in out
    assert recorder.events == []


def test_main_apply_without_key_aborts(tmp_path, monkeypatch):
    path = str(tmp_path / "gateway.db")
    store.init_db(path).close()
    # config builds fine, but no PostHog key → telemetry disabled → must abort,
    # never silently "emit" zero events.
    monkeypatch.setattr(bf, "load_config",
                        lambda env: types.SimpleNamespace(posthog_api_key="", posthog_host=""))
    monkeypatch.setattr(telemetry, "_client", None)
    monkeypatch.setattr(sys, "argv", ["backfill.py", "--db", path, "--apply"])
    with pytest.raises(SystemExit):
        bf.main()
