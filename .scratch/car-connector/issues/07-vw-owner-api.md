# 07 — VW Group owner API availability

Type: research
Status: resolved
Blocked by: —

## Question

Can a private VW (and Audi / Cupra / Seat) owner obtain **official** API access to their own
car today, the way Škoda owners get an `X-API-Key` in the MyŠkoda app? Ticket 01 found no
equivalent public API and a May 2026 shutdown of third-party access to the brand-app API
(secondary press). Dig deeper with primary sources: the brand apps (Volkswagen app, myAudi,
My Cupra/Seat) and their developer portals, the CARIAD / VW Group developer sites, any
"smart home" or API-key feature in those apps, whether the Škoda Public API host family has
sibling brands, and any announced roadmap. Verdict per brand: available now (how, link) /
announced (when) / none. If one is available, note its auth model and whether it fits the
`/car` brand-picker shape (decided in 03) or needs a `car-<brand>` adapter.

## Answer

Researched 2026-10-28. Full facts with per-claim labels are in
[assets/07-vw-owner-api.md](../assets/07-vw-owner-api.md).

- **Verdict:** **none** for Volkswagen, Audi, Cupra/Seat and Porsche. No official owner API
  exists, and none is announced. **VW is not addable this week.**
- **What VW actually published.** VW's only primary notice is the
  [2026-04-02 GIS short news](https://drivesomethinggreater.com/newsroom/short-news/2026-4-02-Transition).
  It closed the brand-app API to external access from calendar week 21. The replacements,
  Charging Data and Charging Control, are offered to **companies** (`gis-sales@cariad.technology`).
  Private owners are not mentioned.
- **VW's statements since.** VW is "working on a joint solution" with the open-source community
  (heise, June 2026), but has given no timeline or product. evcc reported "no concrete result"
  on 2026-08-27. By October 2026 VW had *tightened* access (Play attestation, workarounds
  blocked).
- **Brand apps.** The App Store version histories for the Volkswagen app (4.9.0), myAudi (5.9.0)
  and My CUPRA (2.23.1) have no API-key, developer or smart-home feature like MyŠkoda 8.16's.
- **Official channels are read-only or B2B.**
  - The EU Data Act portal delivers delayed ZIP/JSON exports and has no control.
  - The GIS Data Hub requires ONE Business ID, a framework agreement and a subscription, and
    covers charging only.
  - Neither fits a hosted BYO-key beta with climate actions.
- **No sibling hosts.** The Škoda host is Škoda's own gateway (`msp-gw-myskoda-live` on Azure),
  not a group platform. No `public.api.connect.<brand>` sibling resolves.
- **Fit.** A future key-based VW API slots into the `/car` brand picker. An OAuth/consent-link
  API likely still fits `/car` via a redirect branch (the whoop pattern). Use `car-<brand>` only
  if per-brand OAuth callbacks force it. Watch the evcc and GIS newsroom for a "joint solution"
  announcement.
