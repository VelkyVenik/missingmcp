"""Operator mail campaigns through Cloudflare Email Sending (REST API).

A campaign is created and started by the operator (scripts/campaign.py); this
module drains it from the app lifespan loop — a small batch per tick, capped
per rolling 24h under the provider's quota, so a 3k-recipient announcement goes out
over days without anyone babysitting it. Progress is reported only as
structured log events (campaign-batch / -daily-cap / -auto-paused / -done /
-send-unknown / -run-failed): operator tooling watches the log stream, nothing
is mailed or posted to the operator.

The send ledger (store.campaign_sends) is the safety net: one row per
recipient, `sending` written BEFORE the API call, so a recipient is mailed at
most once — an interrupted or ambiguous send (timeout, 5xx) parks as `unknown`
for the operator to decide (scripts/campaign.py requeue-unknown), never an
automatic re-send. Failures are told apart by whose fault they are: a
recipient-specific rejection (400/422) costs that recipient an attempt and the
batch moves on; an account-wide one (bad token, quota, rate limit, outage)
costs nobody anything and pauses the whole mailer for ERROR_BACKOFF.

Same shape as backup.py / report.py: a `Mailer` with enabled/due/run, whose
run() never raises and works on its own DB connection (it executes in a worker
thread via asyncio.to_thread). Never log a recipient address — provider error
text is passed through `redact` first.
"""
from __future__ import annotations
import re
import sqlite3
import time
from datetime import datetime, timezone
import httpx
from . import store
from .log import log, log_exc, log_warn

BATCH = 10                  # mails per lifespan tick (~60s) — gentle pacing
QUOTA_WINDOW = 24 * 3600    # provider quota is a rolling 24h window
RUN_BUDGET = 30.0          # wall-clock seconds per run — never stall the lifespan loop
MAX_ATTEMPTS = 3            # recipient-specific rejections before it is marked failed
ERROR_BACKOFF = 15 * 60     # seconds to pause after an account-wide error or a maybe-sent
BOUNCE_MIN_ATTEMPTS = 50    # auto-pause guard: only judge after this many sends...
BOUNCE_MAX_RATE = 0.05      # ...and pause above this hard-bounce rate

# 4xx statuses that are about the account or the request rate, not the recipient
_ACCOUNT_WIDE_4XX = {401, 403, 408, 409, 429}
_EMAIL_RE = re.compile(r"[^\s<>\"'@,;:]+@[^\s<>\"'@,;:]+")
_UTC_FMT = "%Y-%m-%d %H:%M:%S"   # matches SQLite datetime('now')


class MailError(Exception):
    """A send that did not succeed.
    `maybe_sent`: the request may have reached the provider (timeout, 5xx) —
    never retry blindly. `recipient_fault`: the provider rejected this
    recipient specifically — retry it a few times, the rest of the batch is
    unaffected. Neither: an account-wide problem — retry later, charge nobody."""

    def __init__(self, message: str, *, maybe_sent: bool = False,
                 recipient_fault: bool = False):
        super().__init__(message)
        self.maybe_sent = maybe_sent
        self.recipient_fault = recipient_fault


def redact(text: str) -> str:
    """Strip email addresses from provider error text before it is logged."""
    return _EMAIL_RE.sub("<email>", text)


def unsubscribe_url(public_url: str, token: str) -> str:
    return f"{public_url}/unsubscribe?t={token}"


def render_text(body: str, unsub_url: str) -> str:
    """Campaign body + the unsubscribe footer (plain text)."""
    return (f"{body.rstrip()}\n\n--\n"
            "You're getting this because you signed up at MissingMCP (missingmcp.com).\n"
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
        raise MailError(f"connect: {type(e).__name__}") from e
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
        raise MailError(f"HTTP {r.status_code}: {errs}",
                        recipient_fault=(400 <= r.status_code < 500
                                         and r.status_code not in _ACCOUNT_WIDE_4XX))
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


def _utc_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(_UTC_FMT)


def _parse_utc(s: str) -> float:
    return datetime.strptime(s, _UTC_FMT).replace(tzinfo=timezone.utc).timestamp()


def open_rw(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


class Mailer:
    """Drains the active campaign from the lifespan loop. Process-local pacing
    state only (next allowed run, last cap log); everything durable lives in
    the ledger, so a redeploy just resumes."""

    def __init__(self, config, clock=time.time, monotonic=time.monotonic):
        self._cfg = config
        self._clock = clock
        self._monotonic = monotonic
        self._next_at = 0.0
        self._cap_logged_at = None

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
            log_exc("campaign-run-failed", e, error=redact(str(e)))
            self._next_at = now + ERROR_BACKOFF

    def _run(self, conn, now: float) -> None:
        stale = store.mark_stale_sending(conn)
        if stale:
            log_warn("campaign-send-unknown", count=stale, reason="interrupted")
        camp = store.active_campaign(conn)
        if camp is None:
            return
        cid, slug = camp["id"], camp["slug"]
        # Cloudflare's daily quota is a ROLLING 24h window, not a UTC day — a
        # calendar-day cap let a late-evening batch plus a post-midnight one
        # overrun it (429 throttled until the evening batch aged out).
        window = _utc_str(now - QUOTA_WINDOW)
        sent_24h = store.sent_since(conn, window)
        room = self._cfg.mail_daily_cap - sent_24h
        if room <= 0:
            # one event per capped stretch, not one per trickle refill
            if self._cap_logged_at is None or now - self._cap_logged_at > QUOTA_WINDOW / 2:
                self._cap_logged_at = now
                log("campaign-daily-cap", campaign=slug, cap=self._cfg.mail_daily_cap,
                    sent_24h=sent_24h, pending=store.campaign_counts(conn, cid)["pending"])
            oldest = store.oldest_sent_since(conn, window)
            # wake when the oldest send ages out of the window
            self._next_at = (_parse_utc(oldest) + QUOTA_WINDOW + 60) if oldest else now + 60
            return

        tally = {"sent": 0, "bounced": 0, "suppressed": 0, "unknown": 0,
                 "retry": 0, "failed": 0}
        error = None
        started = self._monotonic()
        for row in store.next_pending(conn, cid, min(BATCH, room)):
            if self._monotonic() - started > RUN_BUDGET:
                break                                     # rest goes next tick
            email = row["email"]
            url = unsubscribe_url(self._cfg.public_url, row["unsub_token"])
            store.mark_send(conn, cid, email, "sending")
            try:
                result = send(self._cfg, email, camp["subject"], render_text(camp["body"], url),
                              list_unsubscribe_headers(url, self._cfg.mail_reply_to))
            except MailError as e:
                error = redact(str(e))
                if e.maybe_sent:
                    store.mark_send(conn, cid, email, "unknown", error=error,
                                    at=_utc_str(self._clock()))
                    tally["unknown"] += 1
                elif e.recipient_fault:
                    final = row["attempts"] + 1 >= MAX_ATTEMPTS
                    store.mark_send(conn, cid, email, "failed" if final else "pending",
                                    error=error, attempted=True)
                    tally["failed" if final else "retry"] += 1
                    continue                              # this address only
                else:                                     # account-wide: charge nobody
                    store.mark_send(conn, cid, email, "pending", error=error)
                    tally["retry"] += 1
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
                  "sent_24h": store.sent_since(conn, window),
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
