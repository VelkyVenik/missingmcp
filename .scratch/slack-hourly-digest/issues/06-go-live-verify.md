# 06 — Go live & verify (destination)

Type: task
Status: resolved
Blocked by: 05

## Question

Reach the destination: the digest is live and posting.

- Trigger the workflow manually (`workflow_dispatch`) and confirm a message lands in
  the Slack channel.
- Verify both renderings: force/confirm the healthy one-liner, and confirm the
  anomaly path (`@here`, expanded) fires on a window that contains an error/5xx
  (e.g. run against a past window known to contain one, or temporarily lower a
  threshold).
- Confirm the scheduled (hourly) run fires on its own at least once.
- Record: channel, first live message link/screenshot, and any cron-timing drift
  observed. Map is done when the hourly digest is confirmed posting.
