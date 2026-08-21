# Homepage redesign — Garmin-focused wayfinder

Status: ready-for-agent
Date: 2026-08-21 (grilled & locked with Vaclav, 2 rounds, all recommendations accepted)

## Goal

Replace the current inconsistent homepage (Claude/ChatGPT bolted on 2026-08-21)
with a **Garmin-focused wayfinder**: sell Garmin once, show the active-user
count, route to the right client guide. Keep Buy Me a Beer.

## Locked decisions

1. **Scope**: this effort ends with an approved design (structure + copy
   signed off by Vaclav on a prototype); build + PR + deploy is one execution
   session after approval.
2. **Homepage = router.** It sells Garmin in one breath and routes to
   `/garmin` (Claude) and `/garmin/chatgpt` (ChatGPT). Detail — tools, steps,
   caveats — lives on those subpages only. No duplication of their content.
3. **Brand stays MissingMCP** ("the missing piece" story, room for future
   connectors); hero and SEO lead with Garmin.
4. **Claude and ChatGPT get equal billing** — two equivalent cards/buttons,
   Claude first (97 % of traffic). No more bolt-on links.
5. **User count is hero-level**, same single metric & wording as the landing
   pages: "N people used this in the last 30 days" (30-day active accounts per
   ADR-0002, `UsageMeter`, threshold ≥10). No new metrics; "people" stays as
   deliberate marketing simplification of "active accounts".
6. **Bottom "More connectors" strip** (quiet): small WHOOP card (pill Beta,
   cap 10 note), one-liner Rohlík (graduated → official MCP), Oura + Apple
   Health merged into one wishlist sentence — *"On the wishlist — not in
   active development. Want one? Tell me →"* (suggest modal). No "Soon"
   pills, no per-connector Notify-me buttons.
7. **"Just ask" chat demo stays as-is**, answer bubble labelled Claude.
8. **Security & trust stays in full** (4 blocks) — `/#security` is a shared
   anchor linked from the landings; it's the load-bearing answer to the
   credential objection.
9. **One generic 3-step strip stays** (copy URL → sign in once → ask),
   client-agnostic; the dual-client step-2 card dies. Per-client steps live
   on the subpages.
10. **Title/H1 minimal churn**: H1 *"Your Garmin data, in Claude & ChatGPT."*;
    title `MissingMCP — Your Garmin data, in Claude & ChatGPT · Garmin MCP
    Server`. Meta description updated to match.

## New page order

hero (H1 + count + one-liner + [Connect in Claude →] [Connect in ChatGPT →],
quiet "works in any MCP client") → Just ask (unchanged) → 3-step strip →
Security & trust (unchanged) → More connectors strip → beer (unchanged) →
final CTA (repeats the two client buttons).

## Open (react-to-prototype, not decide-in-advance)

- Visual treatment of the hero count (badge vs. big number).
- Exact copy of hero one-liner, wishlist sentence, final CTA.
- Internal anchor renames (`#connectors` → new strip id) — mechanical, check
  all referrers (`final` CTA button, any external links).

## Out of scope

- Any change to `/garmin`, `/garmin/chatgpt`, `/whoop`, `/privacy` content.
- New metrics or meter changes (threshold, window, wording).
- og.png (already multi-client as of 2026-08-21).
