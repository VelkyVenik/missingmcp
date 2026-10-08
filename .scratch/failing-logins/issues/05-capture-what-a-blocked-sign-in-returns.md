# Capture what a blocked sign-in returns

Type: task
Status: open
Blocked by: —

## Question

When a sign-in comes back blocked, what exactly did Garmin's edge answer?
Log (no credentials, no bodies beyond a short classifier) the HTTP status of
the failing widget/portal POST, any `Retry-After` header, and whether the body
is Cloudflare's rate-limit page (error 1015) or a CAS page. This tells
"Choose the throttle policy" whether the limiter is Cloudflare or CAS and how
long its block really lasts (sets the egress cooldown). Small code change via
PR; garminconnect's exceptions may need their response inspected.
