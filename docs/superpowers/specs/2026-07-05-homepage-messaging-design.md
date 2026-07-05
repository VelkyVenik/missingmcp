# MissingMCP Homepage Messaging — Design

**Date:** 2026-07-05
**Status:** Approved copy (pre-implementation).
**Relation to prior specs:** Supersedes the *home page* decision of
`2026-07-05-garmin-finish-and-home-design.md` Part 4 ("sober rozcestník, not a
marketing landing"). Deliberate shift: the gateway is public-facing — anyone
can sign in today, there is no access restriction — so `/` becomes a
benefit-first landing modeled on the structure of wakepins.com. The
rozcestník *function* (list connectors, link to each subpage) is absorbed by
the Connectors section. This spec covers **copy only**; layout/implementation
is a separate plan.

## Decisions (brainstorm 2026-07-05)

| Decision | Choice | Rationale |
|---|---|---|
| Audience | **Public-facing** — written for any visitor, CTA leads to connecting | Sign-in is currently open to anyone; the page should sell the idea and convert, not just route insiders. |
| Design skeleton | wakepins.com homepage structure: hero (badge → short H1 with one highlighted word → subtitle → CTA) → section cards → final CTA → footer | Proven structure the operator already likes; keeps both sites visually kin. |
| Tone | **Benefit-first** — talk about outcomes ("ask about your morning run"), not mechanism | Chosen over dev-tool sober and personal/indie tones. Most accessible to non-technical visitors. |
| Hero angle | **Umbrella with examples** — MissingMCP as a whole, benefit illustrated by concrete per-connector examples | Scales to new connectors without rewriting the hero; connector specifics live in the cards. |
| Sections | Hero → Connectors (cards) → How it works (4 steps) → Security & trust → Final CTA | Prompt-examples section rejected (examples are woven into hero + cards instead). |
| Language | English | Public `.com` domain, existing pages and wakepins are EN. |
| `garmin_mcp` credit | **Not in the homepage footer** — moves to the `/garmin` subpage | The credit is Garmin-specific; the umbrella footer stays connector-neutral. |

## Copy

Placeholders: `{OPERATOR_NAME}` as in the current `landing.html`.
Highlighted words (the wakepins "route" treatment) are marked **[like this]**.

### Hero

- **Badge** (uppercase, tracked): `THE MISSING CONNECTORS`
- **H1:** Your apps, **[in Claude]**.
- **Subtitle:** Ask about last night's sleep. Reorder this week's groceries.
  MissingMCP hosts the connectors your favorite services are missing — sign in
  once, add a URL, and start asking.
- **Primary CTA:** `Browse connectors` (anchor → Connectors section)
- **Secondary link:** `How it works ↓` (anchor → How it works section)

### Connectors

- **Section heading** (uppercase, hairline rule): `Connectors`

**Card: Garmin** — status badge `Live`
> **Garmin**
> Your training, sleep, and health data. Ask how you slept, review
> yesterday's ride, or have Claude plan next week's workouts.
>
> CTA: `Connect Garmin →` (→ `/garmin`)

**Card: Rohlík** — status badge `Coming soon`, no CTA
> **Rohlík**
> Groceries from Claude. Reorder your usuals, turn a recipe into a cart,
> check delivery slots.

**Card: Missing something?** — demand-capture card
> **Missing something?**
> Every connector here started as "I wish Claude could…". Tell me which app
> should be next.
>
> CTA: `Suggest a connector →` (target: see Open questions)

### How it works

- **Section heading** (uppercase): `How it works`
- Four step cards (icon suggestions: `plug`/`list` → `plus-circle` →
  `key-round` → `message-circle`):

1. **Pick a connector** — Choose an app from the list and copy its server URL.
2. **Add it to Claude** — Settings → Connectors → Add custom connector. Works
   on phone, desktop, and web.
3. **Sign in once** — Claude opens a sign-in page — log in with your own
   account. Your password is used once and never stored.
4. **Start asking** — Claude picks up the tools automatically. "How did I
   sleep this week?" just works.

Step 3 deliberately pre-answers the security worry at the moment of maximum
hesitation; the Security section expands on it.

### Security & trust

- **Section heading** (uppercase): `Security & trust`
- **Section lede:** You're signing in with real credentials — here's exactly
  how they're handled.
- Four items (cards/rows with icons):

1. **Password: used once, never stored** — Your password signs you in to the
   service and is immediately discarded. Only the resulting session tokens
   are kept — encrypted with AES-256-GCM.
2. **Standard OAuth 2.1** — Claude never sees your credentials. It gets its
   own revocable token, over the same OAuth + PKCE flow that banks and APIs
   use.
3. **Your data passes through, nothing sticks** — Your health and shopping
   data flows from the service to Claude on demand. The gateway stores none
   of it.
4. **Open source, run by a person** — This instance is run by
   {OPERATOR_NAME}. The full source is on GitHub — audit it, or run your own.

Item 4 replaces the current landing's yellow warning box ("only use if you
trust the operator") with the same substance, positively framed: operator
named + a way out (self-host).

### Final CTA

- **H2:** Give Claude the **[missing]** piece.
- **Subtitle:** Your first connector is two minutes away — free, open source,
  and no server of your own.
- **CTA button:** `Browse connectors` (anchor → Connectors section — keeps
  the umbrella logic; the visitor picks a card)
- **Support line:** Like MissingMCP and want to support it? 🍺 Buy me a beer
  (→ buymeacoffee.com/venik)

The H2 closes the loop with the hero badge and the name — the one place the
brand plays at full volume.

### Meta + footer

- **`<title>`:** MissingMCP — Your apps, in Claude
- **Meta description:** Connect Garmin and more to Claude in two minutes.
  Sign in once, add a URL, start asking. Free and open source.
- **Footer:**
  - Line 1: MissingMCP · The connectors Claude is missing.
  - Line 2: Source on GitHub · Run by {OPERATOR_NAME}
  - (No `garmin_mcp` credit here — it moves to `/garmin`.)

## Out of scope

- Layout, CSS, template implementation (separate plan; wakepins structure is
  the reference).
- `/garmin` subpage copy (existing connect instructions remain; gains the
  "built on garmin_mcp" credit currently in the homepage footer).
- Any access restriction / signup gating.

## Open questions

- **`Suggest a connector` target:** mailto:{OPERATOR_EMAIL} vs a GitHub issue
  on `VelkyVenik/missingmcp` (repo visibility to confirm). Decide at
  implementation time.
