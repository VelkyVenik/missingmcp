"""scripts/backfill_garmin_tokens.py — persists worker-rotated token files
into the store (reliability ticket 05). Loaded via importlib like the other
operator scripts."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import time

from garmin import store

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
SECRET = "s" * 32

spec = importlib.util.spec_from_file_location(
    "scripts_backfill_gt", SCRIPTS / "backfill_garmin_tokens.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


def _seed(tmp_path, key: str, db_blob: str, file_content: str | None,
          conn=None, file_age: float = 0.0):
    """One garmin account with a DB blob and (optionally) a token file whose
    mtime is now+file_age (positive = file newer than the DB row)."""
    conn = conn or store.init_db(str(tmp_path / "gateway.db"))
    store.upsert_account(conn, "garmin", key, db_blob, SECRET)
    if file_content is not None:
        workdir = tmp_path / "users" / key / "tokens"
        workdir.mkdir(parents=True, exist_ok=True)
        f = workdir / "garmin_tokens.json"
        f.write_text(file_content)
        mtime = time.time() + file_age
        os.utime(f, (mtime, mtime))
    return conn


def test_drifted_file_is_persisted_only_with_apply(tmp_path):
    conn = _seed(tmp_path, "a@x.cz", '{"v": 1}', '{"v": 2}', file_age=60)
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=False)
    assert res == {"drifted": ["a@x***"]}
    assert store.get_account_tokens(conn, "garmin", "a@x.cz", SECRET) == '{"v": 1}'
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"persisted": ["a@x***"]}
    assert store.get_account_tokens(conn, "garmin", "a@x.cz", SECRET) == '{"v": 2}'
    # idempotent: the persisted row now reads as in-sync
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"in-sync": ["a@x***"]}


def test_relogin_after_file_write_wins(tmp_path):
    # The DB row is NEWER than the file (user re-signed in after the file was
    # last written): the store must win — persisting the older file would
    # overwrite a fresh login (the gateway's unknown-provenance rule).
    conn = _seed(tmp_path, "b@x.cz", '{"fresh-login": 1}', '{"old-rotation": 1}',
                 file_age=-60)
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"db-newer": ["b@x***"]}
    assert store.get_account_tokens(conn, "garmin", "b@x.cz", SECRET) == '{"fresh-login": 1}'


def test_torn_and_missing_files_never_persist(tmp_path):
    conn = _seed(tmp_path, "c@x.cz", '{"v": 1}', '{"v": 2', file_age=60)  # torn
    _seed(tmp_path, "d@x.cz", '{"v": 1}', None, conn=conn)               # no file
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"torn": ["c@x***"], "no-file": ["d@x***"]}
    assert store.get_account_tokens(conn, "garmin", "c@x.cz", SECRET) == '{"v": 1}'


def test_other_adapters_untouched(tmp_path):
    conn = _seed(tmp_path, "e@x.cz", '{"v": 1}', '{"v": 2}', file_age=60)
    store.upsert_account(conn, "whoop", "e@x.cz", '{"w": 1}', SECRET)
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"persisted": ["e@x***"]}                 # one garmin row only
    assert store.get_account_tokens(conn, "whoop", "e@x.cz", SECRET) == '{"w": 1}'


def test_main_dry_run_output_masks_keys(tmp_path, capsys, monkeypatch):
    _seed(tmp_path, "alice@example.com", '{"v": 1}', '{"v": 2}', file_age=60)
    monkeypatch.setenv("GATEWAY_SECRET", SECRET)
    monkeypatch.setattr(sys, "argv", [
        "backfill_garmin_tokens.py", "--db", str(tmp_path / "gateway.db"),
        "--data-dir", str(tmp_path)])
    bf.main()
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "drifted" in out
    assert "ali***" in out and "alice@example.com" not in out
