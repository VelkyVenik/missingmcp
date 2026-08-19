# 13 — Operator notification channel: move off Slack?

Type: grilling
Status: open

Operator ask (2026-08-19, queued for later): "ten Slack už mě nebaví a
vlastně jediné, co mi tam chodí, jsou ty zprávy — hoď do fronty jiný kanál na
komunikaci. Možná je lepší tohle poslat emailem."

## Question

Everything the gateway tells the operator goes to one Slack workspace the
operator otherwise no longer uses: the daily 08:00 user-stats report
(`report.py`), the hourly hard-signal pager, and (once
[11](11-build-daily-triage.md) ships) the daily triage analysis. Decide the
replacement channel and the migration:

1. **Channel**: e-mail (operator's first instinct — needs an outbound mail
   provider + secrets, which would ALSO remove the main infra objection in
   the user-mail ticket [07](07-mail-channel-decision.md)); a push service
   (e.g. ntfy/Telegram/phone push — better for the hard-signal pager than
   e-mail); or a split (pager → push, daily reports → e-mail).
2. **What migrates**: all three streams, or keep Slack for the pager and move
   only the dailies?
3. **Provider + secrets** (if e-mail): pick one, decide where credentials
   live (Railway env for in-app `report.py`, GH secrets for the Actions
   jobs).
4. **Sequencing**: [11](11-build-daily-triage.md) proceeds with
   `SLACK_WEBHOOK_URL` now, but must keep posting behind one small `notify()`
   seam so this ticket's outcome is a config swap, not a rewrite.

HITL — channel choice, provider account and credentials are the operator's.
