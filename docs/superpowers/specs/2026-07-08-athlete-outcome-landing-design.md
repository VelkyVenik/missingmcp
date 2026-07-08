# Athlete-Focused, Outcome-First Landing — Design

**Date:** 2026-07-08
**Status:** Approved copy direction (pre-implementation).
**Relation to prior specs:** Evolves `2026-07-05-homepage-messaging-design.md`.
That spec positioned MissingMCP as a **generic umbrella** ("your apps, in Claude")
and **explicitly rejected a prompt-examples section**. This spec deliberately
reverses both: it **niches the on-page story to athletes** (the reachable,
highest-potential audience) and **adds a demonstration section** — because a
cross-source "just ask" moment is now the product's core differentiator, not a
nice-to-have. The story narrows; the brand and platform stay generic.

## Why (brainstorm 2026-07-08)

North-star metric is **"Beers per Month"** — people who liked it enough to
support it — not raw sign-ups. Goal: **tens of the right users**, reached first
through the Czech tech scene.

- **Bottleneck:** awareness (A) + comprehension (B), by gut, no hard data.
- **Audience:** highest potential = fitness/health data enthusiasts (**A**);
  actual reach = Czech tech scene (**C**); wedge = **C ∩ A** ("a Czech tech
  person with a Garmin on their wrist").
- **Message (locked):** *"Just ask."* Lead with the outcome; the killer proof
  is **cross-source reasoning** — Garmin supplies expenditure/training, you tell
  Claude what you ate, and Claude puts intake vs. expenditure side by side,
  which no single app does. Optional flourish: it can even act (order the
  shortfall on Rohlík) — shown as a ceiling, not the core (operator does not
  personally rely on it).
- **Anti-jargon principle:** "MCP" is **brand and under-the-hood credibility**
  (also SEO for people who search it), never the entry point. Nothing about MCP
  is deleted; it is moved off the front door.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Positioning | **Athlete / fitness-first** on-page, outcome-led | Matches audience A, the operator's authentic daily use, and the C ∩ A wedge. A focused page converts and shares better than a generic one. |
| Brand & platform | **Stay generic** (name, domain, platform) | Comprehension is won in the content, not the brand; keeping them generic preserves all future optionality. Niche the story, not the identity. |
| Domain | **Keep `missingmcp.com`** (no rebrand); optional alias later | Comprehension lives in the copy, not the URL. Keeping it dissolves naming risk *and* the 25-user migration knot; "MCP" in the domain is a beacon for early-adopter supporters and invisible to share-link visitors. A self-explanatory alias can redirect in later, non-breaking. |
| Information architecture | **Umbrella chrome + vertical body** | Header/footer stay generic ("your data, in Claude"); the page body is athlete vertical #1. Future verticals reframe copy / split the body — design for it, don't build it now. |
| Hero H1 | **Keep** `Your data, [in Claude].` | Already the thesis; do not touch. |
| New section | **Add "Just ask"** — a styled example conversation + "Try asking" chips | Shows the cross-source aha (attract / B) *and* gives the instant first win (retain / conversion). Doubles as a demo until the video (Tah 2) exists. |
| MCP jargon | **Demote to under-the-hood + brand + SEO** | The operator's founding insight: most people don't know what MCP is. |
| Language | **Site EN, LinkedIn CZ** | Keeps the site global-ready; Czech tech reads EN without friction; Czech lands in the share post + screenshots (Tah 2). |
| Measurement | **Gut for now; light watch optional** | Not blocking. See Open questions. |

## Information architecture (umbrella + verticals, built lean)

The operator's model is an **umbrella brand with use-case verticals** (athletes
first, others later). With exactly **one** real vertical today (both live
connectors are fitness), building a vertical framework now is speculative. The
lean implementation:

- **Chrome (header/footer) = the umbrella** — generic promise "your data, in
  Claude", never fitness-specific.
- **Home page body = athlete vertical #1** — the hero subhead, "Just ask"
  demo, and connector cards all speak to athletes.
- **Future split (design-for, not build-now):** when a second vertical exists,
  the athlete body moves to its own page and the home slims to a chooser. The
  generic chrome already fits, so that is a copy/route change, not a rebrand.

## Two layers the page must serve (do not conflate)

1. **Hero demo** — the full cross-source loop. What makes people go "whoa" and
   share it. Serves awareness (A).
2. **First-run win** — what a newcomer can reproduce *on day one* (connect
   Garmin, ask one read-only question). Serves comprehension + conversion (B).

The "Just ask" section carries both: the conversation bubble shows layer 1; the
**first** "Try asking" chip is deliberately the easy single-source read (layer 2).

## Copy

`{OPERATOR}` as today. `[like this]` = the accent-highlighted word.

### Meta

- **`<title>`:** `MissingMCP — Your data, in Claude`
- **Meta description:** *Give Claude your Garmin and health data — then just
  ask. "Did I eat enough for today's ride?" Connect in two minutes. Free and
  open source.*

### Hero (`home.html`) — rewrite subhead only

- **Badge** (uppercase, tracked): recommended `BUILT FOR ATHLETES`
  (alt: `STOP SCROLLING GRAPHS`). Names the niche the *body* embraces.
- **H1 (keep):** Your data, **[in Claude]**.
- **Subhead (rewrite; current one names "connectors / a hosted Garmin MCP
  server"):**
  > Claude can reason about almost anything — except the numbers your apps keep
  > locked away. MissingMCP hands it your real data, starting with Garmin, so
  > you can just ask: *"Did I eat enough for today's ride?"* — and get an answer
  > that actually knows.
- **CTAs (keep):** primary `Connect Garmin →`, outline `Notify me`. The new
  section sits directly below, so it is seen on scroll without a new button.

### NEW section: "Just ask" (insert between Hero and Connectors)

- **Section heading** (uppercase, hairline rule): `JUST ASK`
- **Lede:** No dashboards, no exports. A real question, a real answer — from
  your own data.
- **Example conversation** (styled as chat bubbles, "You" visually distinct
  from "Claude"):
  > **You** — I had chicken and rice for lunch. Am I on track for today's long run?
  >
  > **Claude** — Garmin has you at ~2,850 kcal burned so far today, with a
  > 90-minute run still to come (~950 kcal). Lunch was ~620 kcal — you're
  > running a deficit for the load ahead. Grab a carb-heavy snack before you
  > head out.
- **Caption (under the bubble):** Two sources — your Garmin and what you told
  Claude — connected in one answer. No single app does that.
- **"Try asking" chips** (order is intentional — see "Two layers"):
  1. *How did I sleep this week?* ← instant, single-source first win
  2. *Compare my last three long runs.*
  3. *Why was my recovery low today?*

Visual intent only (bubbles + chips); exact CSS is for the implementation plan.

### Jargon cleanup — `garmin.html`

Reorder outcome-first; keep the honest under-the-hood line (credibility + SEO).

- **Page-hero subhead** — from *"A hosted Garmin MCP server: everything your
  watch knows…"* to:
  > Everything your watch knows — training, sleep, recovery, health — in a
  > conversation. Ask questions, spot trends, and let Claude plan workouts that
  > land back on your wrist. *(A hosted MCP server, under the hood.)*
- **"What is this?" opener** — from *"This connector is a hosted MCP (Model
  Context Protocol) server for Garmin…"* to lead with the outcome, then one
  sentence: *"Under the hood it's a hosted MCP server — but you never have to
  think about that."*

### Footer + Final CTA

- **Footer line 1 (umbrella, generic):** `MissingMCP · Your data, in Claude.`
  (replaces "The connectors Claude is missing." — the tagline is now the
  umbrella promise, not fitness-specific.)
- **Final CTA H2 (keep):** *"Give Claude the [missing] piece."* The pun leans
  on the MissingMCP name — and since the name stays, it stays. One place the
  brand plays at full volume.

## Out of scope (deliberately)

- **Rebrand / new domain** — decided against. An optional self-explanatory
  alias may redirect in later (non-breaking); not now.
- **The 25-user migration message** (garmin.slajs.eu → `missingmcp.com`) —
  separate task; now unblocked (domain is settled).
- **Tah 2** — the LinkedIn demo video.
- **Vertical framework / second use-case page** — design-for, not build-now.
- Security, architecture, connector system, layout framework.

## Open questions

- **Badge wording:** `BUILT FOR ATHLETES` vs `STOP SCROLLING GRAPHS` — decide at
  review.
- **Measurement (optional, non-blocking):** no analytics is wired to this site
  today (the PostHog project on hand is WakePins, not this one). Cheapest signal
  stays the existing `subscribers`/connect counts + beers. A lightweight page
  event could come later if the gut proves unreliable.
