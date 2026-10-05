"""Mail campaigns — audience selection, the send ledger, and the Mailer's
pacing / error handling. The Cloudflare API is faked at httpx.post; the clock is
injected, so nothing reads the wall clock."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from missingmcp import mailer, store
from missingmcp.config import load_config

SECRET = "k" * 40
# 2026-10-05 10:00 UTC
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc).timestamp()


def _cfg(tmp_path, **env):
    base = {"GATEWAY_SECRET": SECRET, "DATA_DIR": str(tmp_path),
            "DB_PATH": str(tmp_path / "gateway.db"), "PUBLIC_URL": "https://gw.example.com",
            "MAIL_API_TOKEN": "tok", "MAIL_ACCOUNT_ID": "acc",
            "MAIL_FROM": "me@news.example.com", "MAIL_FROM_NAME": "Me",
            "MAIL_REPLY_TO": "me@example.com", "MAIL_DAILY_CAP": "180"}
    base.update(env)
    return load_config(base)


def _events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.strip().startswith("{")]


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def _ok(to, bucket="delivered"):
    return _Resp(200, {"success": True, "errors": [],
                       "result": {"message_id": f"<{to}>", "delivered": [], "queued": [],
                                  "permanent_bounces": [], "suppressed_recipients": [],
                                  bucket: [to]}})


@pytest.fixture
def api(monkeypatch):
    """Records every send; `api.reply` maps a recipient to a response or an
    exception (default: delivered)."""
    class Api:
        calls: list[dict] = []
        reply: dict = {}

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            r = self.reply.get(json["to"])
            if isinstance(r, Exception):
                raise r
            return r or _ok(json["to"])
    a = Api()
    a.calls, a.reply = [], {}
    monkeypatch.setattr(mailer.httpx, "post", a.post)
    return a


def _db(tmp_path, people=3):
    conn = store.init_db(str(tmp_path / "gateway.db"))
    for i in range(people):
        store.upsert_account(conn, "garmin", f"u{i}@x.com", "{}", SECRET)
    return conn


def _campaign(conn, slug="c1", status="active"):
    cid = store.create_campaign(conn, slug, "Hello", "Body text.", "all",
                                store.campaign_audience(conn, "all"))
    if status != "draft":
        store.set_campaign_status(conn, cid, status)
    return cid


# --- audience ---------------------------------------------------------------

def test_audience_orders_by_activity_and_excludes_unsubscribed(tmp_path):
    conn = _db(tmp_path, people=0)
    for key in ("idle@x.com", "old@x.com", "new@x.com", "gone@x.com"):
        store.upsert_account(conn, "garmin", key, "{}", SECRET)
    store.upsert_account(conn, "whoop", "new@x.com", "{}", SECRET)   # same person, 2 adapters
    store.add_subscriber(conn, "fan@x.com")
    store.add_subscriber(conn, "old@x.com")                          # user AND subscriber
    rows = [("old@x.com", "get_sleep", "2026-09-01 10:00:00"),
            ("new@x.com", "get_sleep", "2026-10-04 10:00:00"),
            # protocol traffic is not activity: idle@ stays "never active"
            ("idle@x.com", "tools/list", "2026-10-05 09:00:00")]
    for key, tool, last in rows:
        conn.execute("INSERT INTO tool_usage VALUES ('garmin', ?, ?, 1, ?)", (key, tool, last))
    conn.commit()
    store.add_unsubscribe(conn, "gone@x.com", "manual")

    assert store.campaign_audience(conn, "all") == [
        "new@x.com", "old@x.com", "fan@x.com", "idle@x.com"]
    assert store.campaign_audience(conn, "users") == ["new@x.com", "old@x.com", "idle@x.com"]
    assert store.campaign_audience(conn, "subscribers") == ["old@x.com", "fan@x.com"]


def test_unsubscribe_drops_pending_sends_only(tmp_path):
    conn = _db(tmp_path, people=2)
    cid = _campaign(conn)
    store.mark_send(conn, cid, "u0@x.com", "sent")
    store.add_unsubscribe(conn, "u0@x.com", "link")
    store.add_unsubscribe(conn, "u1@x.com", "link")
    store.add_unsubscribe(conn, "u1@x.com", "link")      # idempotent
    counts = store.campaign_counts(conn, cid)
    assert counts["sent"] == 1 and counts["unsubscribed"] == 1 and counts["pending"] == 0


# --- Mailer -----------------------------------------------------------------

def test_disabled_without_mail_config(tmp_path):
    assert not mailer.Mailer(_cfg(tmp_path, MAIL_API_TOKEN="")).enabled
    assert mailer.Mailer(_cfg(tmp_path)).enabled


def test_run_sends_a_batch_with_footer_and_unsubscribe_headers(tmp_path, api, capsys):
    conn = _db(tmp_path, people=2)
    cid = _campaign(conn)
    mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW).run()

    assert [c["json"]["to"] for c in api.calls] == ["u0@x.com", "u1@x.com"]
    call = api.calls[0]
    assert call["url"] == "https://api.cloudflare.com/client/v4/accounts/acc/email/sending/send"
    assert call["headers"] == {"Authorization": "Bearer tok"}
    body = call["json"]
    assert body["from"] == {"address": "me@news.example.com", "name": "Me"}
    assert body["reply_to"] == "me@example.com"
    token = conn.execute("SELECT unsub_token FROM campaign_sends WHERE email='u0@x.com'").fetchone()[0]
    url = f"https://gw.example.com/unsubscribe?t={token}"
    assert body["text"].startswith("Body text.\n\n--\n") and url in body["text"]
    assert body["headers"] == {
        "List-Unsubscribe": f"<{url}>, <mailto:me@example.com?subject=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}

    assert store.campaign_counts(conn, cid)["sent"] == 2
    assert store.get_campaign(conn, "c1")["status"] == "done"
    events = {e["event"]: e for e in _events(capsys)}
    assert events["campaign-batch"]["sent"] == 2
    assert "u0@x.com" not in json.dumps(events)          # never log an address
    assert "campaign-done" in events


def test_bounce_and_suppressed_are_recorded(tmp_path, api):
    conn = _db(tmp_path, people=3)
    cid = _campaign(conn)
    api.reply = {"u0@x.com": _ok("u0@x.com", "permanent_bounces"),
                 "u1@x.com": _ok("u1@x.com", "suppressed_recipients")}
    mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW).run()
    c = store.campaign_counts(conn, cid)
    assert (c["bounced"], c["suppressed"], c["sent"]) == (1, 1, 1)
    # bounced counts toward the daily quota, suppressed doesn't
    assert store.sent_since(conn, "2000-01-01 00:00:00") == 2


def test_batch_size_and_daily_cap(tmp_path, api, capsys):
    conn = _db(tmp_path, people=25)
    cid = _campaign(conn)
    m = mailer.Mailer(_cfg(tmp_path, MAIL_DAILY_CAP="12"), clock=lambda: NOW)
    m.run()
    assert len(api.calls) == mailer.BATCH
    m.run()
    assert len(api.calls) == 12                          # capped, not a full 2nd batch
    m.run()                                              # at cap: logs once, sends nothing
    m.run()
    assert len(api.calls) == 12
    assert store.campaign_counts(conn, cid)["pending"] == 13
    caps = [e for e in _events(capsys) if e["event"] == "campaign-daily-cap"]
    assert len(caps) == 1 and caps[0]["pending"] == 13 and caps[0]["sent_24h"] == 12
    # not due again until the oldest send ages out of the 24h window
    assert not m.due()
    m._clock = lambda: NOW + mailer.QUOTA_WINDOW + 61
    assert m.due()


def test_cap_is_a_rolling_24h_window_not_a_utc_day(tmp_path, api, capsys):
    """Regression: 180 sent late evening + a fresh 'day' after UTC midnight
    overran Cloudflare's rolling quota (429 throttled until evening)."""
    conn = _db(tmp_path, people=30)
    cid = _campaign(conn)
    evening = datetime(2026, 10, 4, 21, 40, tzinfo=timezone.utc).timestamp()
    t = [evening]
    m = mailer.Mailer(_cfg(tmp_path, MAIL_DAILY_CAP="10"), clock=lambda: t[0])
    m.run()
    assert len(api.calls) == 10
    t[0] = datetime(2026, 10, 5, 0, 10, tzinfo=timezone.utc).timestamp()  # past midnight
    m.run()
    assert len(api.calls) == 10                          # still inside the 24h window
    assert m._next_at == evening + mailer.QUOTA_WINDOW + 60
    t[0] = m._next_at
    m.run()
    assert len(api.calls) == 20                          # the evening batch aged out
    assert store.campaign_counts(conn, cid)["pending"] == 10
    caps = [e for e in _events(capsys) if e["event"] == "campaign-daily-cap"]
    assert len(caps) == 1


def _err(status, message):
    return _Resp(status, {"success": False, "errors": [{"code": 1, "message": message}]})


def test_recipient_rejection_retries_that_address_only(tmp_path, api, capsys):
    conn = _db(tmp_path, people=3)
    cid = _campaign(conn)
    api.reply = {"u0@x.com": _err(400, "invalid recipient u0@x.com")}
    m = mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW)
    m.run()
    # the bad address doesn't hold up the batch, nor the mailer
    assert [c["json"]["to"] for c in api.calls] == ["u0@x.com", "u1@x.com", "u2@x.com"]
    assert m.due()
    row = conn.execute("SELECT status, attempts FROM campaign_sends "
                       "WHERE email='u0@x.com'").fetchone()
    assert tuple(row) == ("pending", 1)
    m.run()
    m.run()
    assert conn.execute("SELECT status FROM campaign_sends WHERE email='u0@x.com'"
                        ).fetchone()[0] == "failed"
    out = _events(capsys)
    batches = [e for e in out if e["event"] == "campaign-batch"]
    assert batches[0]["level"] == "warn" and batches[-1]["failed"] == 1
    assert "u0@x.com" not in json.dumps(out)              # provider text is redacted
    assert "<email>" in batches[0]["error"]
    assert store.get_campaign(conn, "c1")["status"] == "done"
    assert store.requeue(conn, cid, "failed") == 1
    assert conn.execute("SELECT attempts FROM campaign_sends WHERE email='u0@x.com'"
                        ).fetchone()[0] == 0


@pytest.mark.parametrize("status", [401, 403, 429])
def test_account_wide_error_charges_nobody_and_backs_off(tmp_path, api, status):
    conn = _db(tmp_path, people=2)
    cid = _campaign(conn)
    api.reply = {"u0@x.com": _err(status, "nope")}
    t = [NOW]
    m = mailer.Mailer(_cfg(tmp_path), clock=lambda: t[0])
    for _ in range(5):                                    # a long outage...
        m.run()
        assert not m.due()
        t[0] += mailer.ERROR_BACKOFF
    assert len(api.calls) == 5                            # one probe per backoff
    row = conn.execute("SELECT status, attempts FROM campaign_sends "
                       "WHERE email='u0@x.com'").fetchone()
    assert tuple(row) == ("pending", 0)                   # ...burns no recipient
    assert store.campaign_counts(conn, cid)["failed"] == 0


def test_run_budget_stops_a_slow_batch(tmp_path, api):
    conn = _db(tmp_path, people=5)
    _campaign(conn)
    ticks = iter([0, 0, 10, 20, 31, 40])                  # monotonic seconds per check
    m = mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW, monotonic=lambda: next(ticks))
    m.run()
    assert len(api.calls) == 3                            # 4th check is past RUN_BUDGET


def test_unsubscribe_after_requeue_is_never_mailed(tmp_path, api):
    conn = _db(tmp_path, people=2)
    cid = _campaign(conn)
    store.mark_send(conn, cid, "u0@x.com", "unknown")
    store.mark_send(conn, cid, "u1@x.com", "failed")
    store.add_unsubscribe(conn, "u1@x.com", "link")       # drops failed rows too
    store.requeue(conn, cid, "unknown")
    store.add_unsubscribe(conn, "u0@x.com", "link")       # after the requeue...
    conn.execute("UPDATE campaign_sends SET status='pending' WHERE email='u0@x.com'")
    conn.commit()                                         # ...even if a row slipped back
    mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW).run()
    assert api.calls == []
    assert store.campaign_counts(conn, cid)["unsubscribed"] == 2


def test_unknown_counts_toward_the_daily_cap(tmp_path, api):
    conn = _db(tmp_path, people=4)
    cid = _campaign(conn)
    api.reply = {"u0@x.com": httpx.ReadTimeout("slow")}
    m = mailer.Mailer(_cfg(tmp_path, MAIL_DAILY_CAP="2"), clock=lambda: NOW)
    m.run()
    m._next_at = 0                                        # skip the backoff
    m.run()
    assert len(api.calls) == 2                            # unknown + 1 sent = cap
    assert store.campaign_counts(conn, cid)["pending"] == 2


def test_ambiguous_send_parks_as_unknown_and_is_never_resent(tmp_path, api):
    conn = _db(tmp_path, people=2)
    cid = _campaign(conn)
    api.reply = {"u0@x.com": httpx.ReadTimeout("slow")}
    t = [NOW]
    m = mailer.Mailer(_cfg(tmp_path), clock=lambda: t[0])
    m.run()
    t[0] += mailer.ERROR_BACKOFF
    m.run()
    assert [c["json"]["to"] for c in api.calls] == ["u0@x.com", "u1@x.com"]
    assert store.unknown_sends(conn, cid) == ["u0@x.com"]
    assert store.get_campaign(conn, "c1")["status"] == "done"   # nothing pending
    assert store.requeue(conn, cid, "unknown") == 1


def test_crash_mid_send_becomes_unknown(tmp_path, api, capsys):
    conn = _db(tmp_path, people=1)
    cid = _campaign(conn)
    api.reply = {"u0@x.com": RuntimeError("process died")}
    t = [NOW]
    m = mailer.Mailer(_cfg(tmp_path), clock=lambda: t[0])
    m.run()                                              # never raises
    assert conn.execute("SELECT status FROM campaign_sends").fetchone()[0] == "sending"
    t[0] += mailer.ERROR_BACKOFF
    m.run()
    assert store.unknown_sends(conn, cid) == ["u0@x.com"]
    assert len(api.calls) == 1
    # a crash-interrupted send may have reached the provider: it counts toward the quota
    assert store.sent_since(conn, "2000-01-01 00:00:00") == 1
    names = [e["event"] for e in _events(capsys)]
    assert "campaign-run-failed" in names and "campaign-send-unknown" in names


def test_high_bounce_rate_auto_pauses(tmp_path, api, capsys, monkeypatch):
    monkeypatch.setattr(mailer, "BATCH", 30)
    conn = _db(tmp_path, people=80)
    cid = _campaign(conn)
    first = store.campaign_audience(conn, "all")[:4]
    api.reply = {e: _ok(e, "permanent_bounces") for e in first}
    m = mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW)
    m.run()                          # 4/30 bounced: too few sends to judge yet
    assert store.get_campaign(conn, "c1")["status"] == "active"
    m.run()                          # 4/60 = 6.7% > 5% → paused
    assert store.get_campaign(conn, "c1")["status"] == "paused"
    paused = [e for e in _events(capsys) if e["event"] == "campaign-auto-paused"]
    assert paused[0]["bounced"] == 4 and paused[0]["attempted"] == 60
    m.run()
    assert len(api.calls) == 60 and store.campaign_counts(conn, cid)["pending"] == 20


def test_no_active_campaign_is_a_no_op(tmp_path, api):
    conn = _db(tmp_path, people=2)
    _campaign(conn, status="draft")
    mailer.Mailer(_cfg(tmp_path), clock=lambda: NOW).run()
    assert api.calls == []


def test_zero_cap_is_a_quiet_kill_switch(tmp_path, api, capsys):
    conn = _db(tmp_path, people=2)
    _campaign(conn)
    m = mailer.Mailer(_cfg(tmp_path, MAIL_DAILY_CAP="0"), clock=lambda: NOW)
    m.run()
    assert api.calls == [] and m._next_at == NOW + 3600     # hourly, not every tick


def test_cap_event_logged_again_for_a_different_campaign(tmp_path, api, capsys):
    conn = _db(tmp_path, people=4)
    a = _campaign(conn, "a")
    t = [NOW]
    m = mailer.Mailer(_cfg(tmp_path, MAIL_DAILY_CAP="2"), clock=lambda: t[0])
    m.run()
    m.run()                                                 # a: capped → event
    store.set_campaign_status(conn, a, "paused")
    store.create_campaign(conn, "b", "Hi", "Body", "all", ["u0@x.com"])
    store.set_campaign_status(conn, store.get_campaign(conn, "b")["id"], "active")
    t[0] += 3600
    m.run()                                                 # b: capped → its own event
    caps = [e["campaign"] for e in _events(capsys) if e["event"] == "campaign-daily-cap"]
    assert caps == ["a", "b"]
