"""Beer-supporters feature: the `beers` store helpers and scripts/add_beer.py's
attribution + `beer_purchased` emit (design: 2026-07-24-beer-supporters).

Telemetry is exercised through a stubbed SDK client (`telemetry._client`) — the
same pattern as test_telemetry.py; the real PostHog SDK is never touched. The
script is standalone (not a package), so it's loaded via importlib like
test_scripts.py and its functions driven directly / through main().
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

from garmin import store, telemetry

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
SECRET = "s" * 40
DT = datetime(2026, 7, 20, tzinfo=timezone.utc)   # a fixed purchase time


def _load_add_beer():
    spec = importlib.util.spec_from_file_location("scripts_add_beer", SCRIPTS / "add_beer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


add_beer = _load_add_beer()


class Recorder:
    """Stand-in for telemetry._client: records capture() calls (incl. the new
    timestamp kwarg) instead of shipping them."""

    def __init__(self):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None, timestamp=None):
        self.events.append({"event": event, "distinct_id": distinct_id,
                            "properties": properties, "timestamp": timestamp})

    def shutdown(self):   # main() flushes on exit
        pass


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


# --- store helpers -----------------------------------------------------------

def test_account_key_exists_true_false(conn):
    assert not store.account_key_exists(conn, "me@x.cz")
    store.upsert_account(conn, "garmin", "me@x.cz", "{}", SECRET)
    assert store.account_key_exists(conn, "me@x.cz")


def test_account_key_exists_matches_any_adapter(conn):
    # attribution keys on the email alone (the cross-adapter join key)
    store.upsert_account(conn, "whoop", "w@x.cz", "{}", SECRET)
    assert store.account_key_exists(conn, "w@x.cz")


def test_add_beer_inserts_row_and_returns_id(conn):
    rid = store.add_beer(conn, email="me@x.cz", beers=3, amount=15.0, currency="EUR",
                         matched=1, source="manual", created_at="2026-07-20 00:00:00")
    assert isinstance(rid, int) and rid > 0
    row = conn.execute("SELECT * FROM beers WHERE id=?", (rid,)).fetchone()
    assert row["email"] == "me@x.cz"
    assert row["beers"] == 3
    assert row["amount"] == 15.0
    assert row["currency"] == "EUR"
    assert row["matched"] == 1
    assert row["source"] == "manual"
    assert row["created_at"] == "2026-07-20 00:00:00"


def test_add_beer_anonymous_stores_null_email(conn):
    rid = store.add_beer(conn, email=None, beers=1, amount=5, currency="EUR",
                         matched=0, source="manual", created_at="2026-07-24 10:00:00")
    row = conn.execute("SELECT email, matched FROM beers WHERE id=?", (rid,)).fetchone()
    assert row["email"] is None and row["matched"] == 0


# --- attribution outcomes (record_beer) --------------------------------------

def test_record_beer_matched(conn, recorder):
    store.upsert_account(conn, "garmin", "honza@example.cz", "{}", SECRET)
    res = add_beer.record_beer(conn, email="Honza@Example.cz", beers=1, amount=5,
                               currency="EUR", created_at=DT)          # note: mixed case
    assert res["matched"] is True
    assert res["distinct_id"] == "honza@example.cz"                    # normalized
    row = conn.execute("SELECT email, matched FROM beers").fetchone()
    assert row["email"] == "honza@example.cz" and row["matched"] == 1
    ev = recorder.events[0]
    assert ev["event"] == "beer_purchased"
    assert ev["distinct_id"] == "honza@example.cz"
    assert ev["properties"]["matched"] is True


def test_record_beer_unmatched_with_email(conn, recorder):
    res = add_beer.record_beer(conn, email="stranger@example.cz", beers=2, amount=10,
                               currency="EUR", created_at=DT)
    assert res["matched"] is False
    assert res["distinct_id"] == "stranger@example.cz"                 # still keyed on email
    assert conn.execute("SELECT matched FROM beers").fetchone()["matched"] == 0
    ev = recorder.events[0]
    assert ev["distinct_id"] == "stranger@example.cz"
    assert ev["properties"]["matched"] is False


def test_record_beer_anonymous_synthetic_id(conn, recorder):
    res = add_beer.record_beer(conn, email=None, beers=1, amount=5,
                               currency="EUR", created_at=DT)
    assert res["matched"] is False
    assert res["distinct_id"] == f"manual:anon:{res['id']}"
    assert conn.execute("SELECT email FROM beers").fetchone()["email"] is None
    assert recorder.events[0]["distinct_id"] == f"manual:anon:{res['id']}"


# --- event shape: properties + backdated timestamp + egress ------------------

def test_record_beer_event_properties_and_timestamp(conn, recorder):
    add_beer.record_beer(conn, email="me@x.cz", beers=3, amount=15,
                         currency="USD", created_at=DT)
    ev = recorder.events[0]
    assert ev["properties"] == {"beers": 3, "amount": 15, "currency": "USD",
                                "matched": False, "source": "manual"}
    assert ev["timestamp"] == DT                       # backdated to the purchase time
    # egress rule: email only as distinct_id; never a name/note in properties
    assert "email" not in ev["properties"]
    assert "name" not in ev["properties"] and "note" not in ev["properties"]


# --- no-op contract: telemetry off still writes the row ----------------------

def test_record_beer_writes_row_when_telemetry_disabled(conn, monkeypatch):
    monkeypatch.setattr(telemetry, "_client", None)    # POSTHOG_API_KEY unset
    res = add_beer.record_beer(conn, email="me@x.cz", beers=1, amount=5,
                               currency="EUR", created_at=DT)          # must not raise
    assert conn.execute("SELECT COUNT(*) FROM beers").fetchone()[0] == 1
    assert res["distinct_id"] == "me@x.cz"


# --- end-to-end through main() ----------------------------------------------

def test_main_records_prints_and_emits(tmp_path, capsys, monkeypatch, recorder):
    path = str(tmp_path / "gateway.db")
    c = store.init_db(path)
    store.upsert_account(c, "garmin", "honza@example.cz", "{}", SECRET)
    c.close()
    monkeypatch.setenv("GATEWAY_SECRET", "z" * 40)     # load_config needs it
    monkeypatch.setattr(sys, "argv",
                        ["add_beer.py", "--db", path, "--email", "Honza@Example.cz",
                         "--beers", "3", "--at", "2026-07-20"])
    add_beer.main()

    out = capsys.readouterr().out
    assert "Recorded 3 beer(s) (15 EUR) for honza@example.cz" in out
    assert "matched" in out and "beers.id=" in out

    ro = sqlite3.connect(path)
    row = ro.execute("SELECT email, beers, amount, currency, matched, created_at "
                     "FROM beers").fetchone()
    ro.close()
    assert row == ("honza@example.cz", 3, 15.0, "EUR", 1, "2026-07-20 00:00:00")

    ev = recorder.events[0]
    assert ev["event"] == "beer_purchased" and ev["distinct_id"] == "honza@example.cz"
    assert ev["timestamp"] == datetime(2026, 7, 20, tzinfo=timezone.utc)


def test_main_requires_email(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["add_beer.py", "--db", "/nope.db"])
    with pytest.raises(SystemExit):
        add_beer.main()


def test_main_rejects_blank_email(monkeypatch):
    # argparse accepts --email "   "; our guard must reject it before any write
    monkeypatch.setattr(sys, "argv", ["add_beer.py", "--db", "/nope.db", "--email", "   "])
    with pytest.raises(SystemExit):
        add_beer.main()


def test_main_rejects_nonpositive_amount(tmp_path, monkeypatch):
    path = str(tmp_path / "gateway.db")
    store.init_db(path).close()
    monkeypatch.setattr(sys, "argv",
                        ["add_beer.py", "--db", path, "--email", "me@x.cz", "--amount=-5"])
    with pytest.raises(SystemExit):
        add_beer.main()
    ro = sqlite3.connect(path)
    n = ro.execute("SELECT COUNT(*) FROM beers").fetchone()[0]
    ro.close()
    assert n == 0    # rejected before any write
