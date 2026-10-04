"""Operator mail campaigns through Cloudflare Email Sending (REST API).

A campaign is created and started by the operator (scripts/campaign.py); this
module drains it from the app lifespan loop — a small batch per tick, capped
per UTC day under the provider's quota, so a 3k-recipient announcement goes out
over days without anyone babysitting it. Progress is reported only as
structured log events (campaign-batch / -daily-cap / -auto-paused / -done /
-send-unknown / -run-failed): operator tooling watches the log stream, nothing
is mailed or posted to the operator.

The send ledger (store.campaign_sends) is the safety net: one row per
recipient, `sending` written BEFORE the API call, so a recipient is mailed at
most once — an interrupted or ambiguous send (timeout, 5xx) parks as `unknown`
for the operator to decide (scripts/campaign.py requeue-unknown), never an
automatic re-send.

Same shape as backup.py / report.py: a `Mailer` with enabled/due/run, whose
run() never raises and works on its own DB connection (it executes in a worker
thread via asyncio.to_thread). Never log a recipient address.
"""
from __future__ import annotations
import sqlite3
import time
from datetime import datetime, timedelta, timezone
import httpx
from . import store
from .log import log, log_exc, log_warn

BATCH = 10                  # mails per lifespan tick (~60s) — gentle pacing
MAX_ATTEMPTS = 3            # API rejections before a recipient is marked failed
ERROR_BACKOFF = 15 * 60     # seconds to pause after any API error (quota, outage)
BOUNCE_MIN_ATTEMPTS = 50    # auto-pause guard: only judge after this many sends...
BOUNCE_MAX_RATE = 0.05      # ...and pause above this hard-bounce rate


class MailError(Exception):
    """A send that did not succeed. `maybe_sent` = the request may have reached
    the provider (timeout, 5xx) — the recipient must not be retried blindly."""

    def __init__(self, message: str, *, maybe_sent: bool):
        super().__init__(message)
        self.maybe_sent = maybe_sent


def unsubscribe_url(public_url: str, token: str) -> str:
    return f"{public_url}/unsubscribe?t={token}"


def render_text(body: str, unsub_url: str) -> str:
    """Campaign body + the unsubscribe footer (plain text)."""
    return (f"{body.rstrip()}\n\n--\n"
            "You're getting this because you use MissingMCP (missingmcp.com).\n"
            f"Unsubscribe: {unsub_url}\n")


def list_unsubscribe_headers(unsub_url: str, reply_to: str) -> dict:
    """RFC 2369 + RFC 8058 one-click headers (Gmail/Yahoo bulk-sender rules)."""
    targets = [f"<{unsub_url}>"]
    if reply_to:
        targets.append(f"<mailto:{reply_to}?subject=unsubscribe>")
    return {"List-Unsubscribe": ", ".join(targets),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def send(config, to: str, subject: str, text: str, headers: dict | None = None) -> dict:
    """Blocking POST of one mail. Returns the API `result` (delivered / queued /
    permanent_bounces / suppressed_recipients / message_id); raises MailError."""
    payload = {"to": to, "subject": subject, "text": text,
               "from": ({"address": config.mail_from, "name": config.mail_from_name}
                        if config.mail_from_name else config.mail_from)}
    if config.mail_reply_to:
        payload["reply_to"] = config.mail_reply_to
    if headers:
        payload["headers"] = headers
    url = f"{config.mail_api_base}/accounts/{config.mail_account_id}/email/sending/send"
    try:
        r = httpx.post(url, json=payload, timeout=20.0,
                       headers={"Authorization": f"Bearer {config.mail_api_token}"})
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise MailError(f"connect: {e}", maybe_sent=False) from e
    except httpx.HTTPError as e:   # timeout / reset after the request left
        raise MailError(f"transport: {type(e).__name__}", maybe_sent=True) from e
    if r.status_code >= 500:
        raise MailError(f"HTTP {r.status_code}", maybe_sent=True)
    try:
        data = r.json()
    except ValueError:
        raise MailError(f"HTTP {r.status_code}: non-JSON body",
                        maybe_sent=r.status_code < 400) from None
    if r.status_code >= 400 or not data.get("success"):
        errs = "; ".join(f"{e.get('code')}: {e.get('message')}"
                         for e in data.get("errors") or []) or f"HTTP {r.status_code}"
        raise MailError(errs, maybe_sent=False)
    return data.get("result") or {}


def classify(result: dict, to: str) -> str:
    """Ledger status for one recipient from the API result."""
    def has(key):
        return to.lower() in {a.lower() for a in result.get(key) or []}
    if has("permanent_bounces"):
        return "bounced"
    if has("suppressed_recipients"):
        return "suppressed"
    if has("delivered") or has("queued"):
        return "sent"
    return "unknown"


_UTC_FMT = "%Y-%m-%d %H:%M:%S"   # matches SQLite datetime('now')


def _utc_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(_UTC_FMT)


def _utc_day_start(now: float) -> datetime:
    return datetime.fromtimestamp(now, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)


def open_rw(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


class Mailer:
    """Drains the active campaign from the lifespan loop. Process-local pacing
    state only (next allowed run, last capped day); everything durable lives in
    the ledger, so a redeploy just resumes."""

    def __init__(self, config, clock=time.time):
        self._cfg = config
        self._clock = clock
        self._next_at = 0.0
        self._capped_day = None

    @property
    def enabled(self) -> bool:
        c = self._cfg
        return bool(c.mail_api_token and c.mail_account_id and c.mail_from)

    def due(self) -> bool:
        return self._clock() >= self._next_at

    def run(self) -> None:   # blocking: via asyncio.to_thread
        now = self._clock()
        try:
            conn = open_rw(self._cfg.db_path)
            try:
                self._run(conn, now)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 - mail must never take the gateway down
            log_exc("campaign-run-failed", e, error=str(e))
            self._next_at = now + ERROR_BACKOFF

    def _run(self, conn, now: float) -> None:
        stale = store.mark_stale_sending(conn)
        if stale:
            log_warn("campaign-send-unknown", count=stale, reason="interrupted")
        camp = store.active_campaign(conn)
        if camp is None:
            return
        cid, slug = camp["id"], camp["slug"]
        day = _utc_day_start(now)
        sent_today = store.sent_since(conn, day.strftime(_UTC_FMT))
        room = self._cfg.mail_daily_cap - sent_today
        if room <= 0:
            if self._capped_day != day:
                self._capped_day = day
                log("campaign-daily-cap", campaign=slug, cap=self._cfg.mail_daily_cap,
                    sent_today=sent_today, pending=store.campaign_counts(conn, cid)["pending"])
            self._next_at = (day + timedelta(days=1, minutes=5)).timestamp()
            return

        tally = {"sent": 0, "bounced": 0, "suppressed": 0, "unknown": 0,
                 "retry": 0, "failed": 0}
        error = None
        for row in store.next_pending(conn, cid, min(BATCH, room)):
            email = row["email"]
            url = unsubscribe_url(self._cfg.public_url, row["unsub_token"])
            store.mark_send(conn, cid, email, "sending")
            try:
                result = send(self._cfg, email, camp["subject"], render_text(camp["body"], url),
                              list_unsubscribe_headers(url, self._cfg.mail_reply_to))
            except MailError as e:
                error = str(e)
                if e.maybe_sent:
                    store.mark_send(conn, cid, email, "unknown", error=error)
                    tally["unknown"] += 1
                else:
                    final = row["attempts"] + 1 >= MAX_ATTEMPTS
                    store.mark_send(conn, cid, email, "failed" if final else "pending",
                                    error=error, attempted=True)
                    tally["failed" if final else "retry"] += 1
                self._next_at = now + ERROR_BACKOFF
                break
            status = classify(result, email)
            store.mark_send(conn, cid, email, status, message_id=result.get("message_id"),
                            at=_utc_str(self._clock()))
            tally[status] += 1

        if not any(tally.values()):
            self._finish_if_drained(conn, cid, slug)
            return
        counts = store.campaign_counts(conn, cid)
        fields = {"campaign": slug, **tally,
                  "sent_today": sent_today + tally["sent"] + tally["bounced"],
                  "pending": counts["pending"], "total_sent": counts["sent"],
                  "total_bounced": counts["bounced"]}
        if error:
            log_warn("campaign-batch", **fields, error=error)
        else:
            log("campaign-batch", **fields)

        attempted = counts["sent"] + counts["bounced"]
        if (attempted >= BOUNCE_MIN_ATTEMPTS
                and counts["bounced"] / attempted > BOUNCE_MAX_RATE):
            store.set_campaign_status(conn, cid, "paused")
            log_warn("campaign-auto-paused", campaign=slug, reason="bounce-rate",
                     bounced=counts["bounced"], attempted=attempted)
            return
        self._finish_if_drained(conn, cid, slug, counts)

    def _finish_if_drained(self, conn, cid, slug, counts=None) -> None:
        counts = counts or store.campaign_counts(conn, cid)
        if counts["pending"] == 0 and counts["sending"] == 0:
            store.set_campaign_status(conn, cid, "done")
            log("campaign-done", campaign=slug,
                **{k: v for k, v in counts.items() if k not in ("pending", "sending")})
