# Footer: one row, brand left, links right

Status: ready-for-agent

## Problem

The site-wide footer (`templates/_layout.html`, `<footer>`) stacks three left-aligned lines
under the centered final CTA box ("Your car has the answers."). It reads as misaligned on
every connector page (operator, 2026-10-28, seen on `/car`).

## Decision (operator)

Option **b**: one row on desktop. Left: "MissingMCP · Your data, in Claude. Built by Vaclav
Slajs." plus "This instance is run by {OPERATOR}." Right: the links (Source on GitHub ·
Privacy · Buy me a beer). On phones it wraps to stacked lines (flex-wrap), left-aligned.

## Scope

- `_layout.html` only: footer markup (keep the `{OPERATOR}` placeholder and every link/text)
  plus CSS (`footer .wrap` → flex, `justify-content: space-between`, `align-items: center`,
  `flex-wrap: wrap`, gap). No copy changes.
- Check it at phone width (16px gutters, no horizontal scroll) and on `/`, `/garmin`, `/whoop`,
  `/car`, `/privacy`, and the 404 page.
- Existing page tests stay green. Add a test only if one already pins the footer structure.

## Notes

Shared layout, so it changes every page; ship as its own small PR (not bundled with a
connector change).
