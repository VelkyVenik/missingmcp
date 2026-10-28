# Garmin connect guides: fix ChatGPT, add Grok, refresh Claude

Status: done (2026-10-28) — live Garmin sign-in in Grok still to confirm after deploy

## Problem

The Garmin connect instructions have drifted from the clients' current UIs. This came up while
researching connect steps for the car connector (2026-10-28, sources below).

- **ChatGPT** (`/garmin/chatgpt`, `templates/garmin_chatgpt.html`): the page says
  *Settings → Apps → Advanced settings → Developer mode → Create app* and
  "Plus, Pro, Business, Enterprise, Education". OpenAI has moved custom MCP servers to
  **chatgpt.com/plugins → + → Add custom MCP server**: name, public endpoint, URL, Authentication:
  OAuth, accept the risk warning, then **Create as a plugin** and install it. The plan list isn't
  backed by current official docs; secondary sources say Plus/Pro may get read-only. Still
  accurate: web-only, OAuth, and the confirmation before write tools.
- **Grok**: no guide exists. grok.com supports custom remote MCP connectors:
  **grok.com/connectors → New Connector → Custom → URL → complete sign-in**. On Grok Business, an
  admin adds it first in console.x.ai → Grok Business → Connectors. Grok's handling of DCR and our
  path-scoped discovery is undocumented, so it **must be tested live** before publishing.
  *Update 2026-10-28:* Vaclav confirmed Grok works live with `/car` (same OAuth layer), so a
  single Garmin sign-in in Grok is the remaining check.
- **Claude** (`templates/garmin.html` step 2 and the home page if it repeats it): the page says
  *Settings → Connectors → Add custom connector … leave OAuth Client ID/Secret empty*. Claude now
  shows *Customize → Connectors → + Add → Add custom connector*, and an OAuth client choice whose
  default is "Use Claude's published identity" (CIMD), which we don't support. Users need
  **"Register automatically"** (DCR). Team/Enterprise: an owner adds it under Organization
  settings → Connectors, then members click Connect.

The car connector's landing page (`templates/car.html`, PR #65) already carries the updated steps
for all three clients and can serve as the reference copy.

## Scope

1. Rewrite the ChatGPT steps and the "Good to know" note in `garmin_chatgpt.html` (fallback hint:
   "look for Plugins / Connectors / Apps"). Drop the unverified plan list or replace it with
   "availability depends on your plan".
2. Add `/garmin/grok`: a new `templates/garmin_grok.html` in the same skeleton as the ChatGPT
   guide. Wire it in `app.py` like `garmin_chatgpt` (route, JSON-LD, `_fill_meters`), add it to
   the sitemap list next to `/garmin/chatgpt` and to `llms.txt`. Add a "Using Grok?" pointer on
   `garmin.html` next to the existing ChatGPT one.
3. Update the Claude step in `garmin.html` (menu path + "Register automatically"), and the same
   copy wherever else it's repeated (grep for "Add custom connector").
4. Home page (`home.html` + the home title/description in `app.py`): H1, title and meta read
   "Claude, ChatGPT & Grok" like `/car`; a third hero/final CTA button → `/garmin/grok`.
5. Tests in the existing landing-page style: `/garmin/grok` renders, and the sitemap includes it.

## Acceptance

- **Manual, before merge:** add `{PUBLIC_URL}/garmin/mcp` in ChatGPT (plugins flow) and in Grok
  (grok.com/connectors) and complete the sign-in. If Grok can't complete DCR or path-scoped
  discovery, stop and report; don't publish a Grok guide that doesn't work.
- The Claude flow works with "Register automatically".
- Suite green.

## Sources

- Claude: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- ChatGPT: https://developers.openai.com/plugins/deploy/connect-chatgpt ;
  auth (CIMD preferred, DCR supported): https://developers.openai.com/plugins/build/auth
- Grok: https://docs.x.ai/grok/connectors ; admin: https://docs.x.ai/grok/connector-management
- Unverified: ChatGPT plan tiers; Grok tiers, mobile and DCR; whether Claude falls back to DCR
  automatically when CIMD isn't advertised.
