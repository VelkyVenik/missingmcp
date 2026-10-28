# 07 — VW Group owner API: facts (researched 2026-10-28)

Question: can a private Volkswagen / Audi / Cupra / Seat owner get **official** API access to
their own car today, the way a Škoda owner gets an `X-API-Key` in the MyŠkoda app? Is VW
addable "this week"?

Labels: **[primary]** = the vendor's own page, app listing or live probe; **[secondary]** =
press / community reporting; **[inference]** = my conclusion.

## Verdict

| Brand | Verdict | Owner auth model | Fit with `/car` |
| --- | --- | --- | --- |
| **Volkswagen** | **None.** Nothing announced, no date. | — | n/a today |
| **Audi** | **None.** Nothing announced. | — | n/a today |
| **Cupra / Seat** | **None.** Nothing announced. | — | n/a today |
| Porsche | None (separate stack; only unofficial libs). | — | n/a |

**VW is not addable this week.** No VW Group brand except Škoda offers an owner-facing API
key or an official developer API for private owners. The only official channels are a
**read-only, delayed data-export portal** (EU Data Act) and **B2B partner APIs**
(GIS Data Hub). Neither fits the hosted BYO-key beta. **[inference, from everything below]**

## 1. What VW officially said and did

- **[primary]** VW Group Info Services short news, 2026-04-02, *"Technical update: Transition
  to next-generation vehicle data interfaces"*
  (<https://drivesomethinggreater.com/newsroom/short-news/2026-4-02-Transition>):
  - the existing brand-app interface "for external access" closes **from calendar week 21**;
  - named replacements are **Charging Data** and **Charging Control**;
  - it is addressed to **companies**, with contact `gis-sales@cariad.technology`;
  - "ten charging data providers" had already integrated.
  - It says nothing about private owners, the open-source community or an owner API.
- **[secondary]** VW's statement as quoted by heise, 9 June 2026
  ([11326538](https://www.heise.de/en/news/E-car-drivers-frustrated-VW-interface-for-third-party-providers-gone-11326538.html),
  also in
  [borncity part 4](https://borncity.com/blog/2026/06/20/nachlese-zum-problem-der-vw-api-aenderung-fuer-e-auto-ladesteuerung-stellungnahme-von-vw-interview-mit-tibber-und-weitere-insights/)):
  - VW has been "in exchange since autumn 2025" with the open-source community and is
    "working on a joint solution";
  - it is "examining additional access routes";
  - **no timeline**, and no concrete product named.
- **[secondary]** VW's replies to owners, posted in
  [evcc discussion #30236](https://github.com/evcc-io/evcc/discussions/30236) (5 and 13 June):
  - the Data Act does not prescribe "a specific technical means of provision";
  - third parties must be **onboarded first**;
  - unauthorized access is "technisch unterbunden" (technically blocked).
- **[secondary]** The evcc team, which is the counterpart in those talks, on 2026-08-27
  ([evcc blog](https://docs.evcc.io/en/blog/2026/08/27/skoda-vehicle-api)): talks with "the
  VW brand for about a year now, so far without a concrete result". The post names no other
  group brand.
- **[secondary]** The enforcement keeps tightening:
  - in [HA volkswagencarnet #977](https://github.com/robinostlund/homeassistant-volkswagencarnet/issues/977),
    a project collaborator reported on **2026-10-24** that VW blocked the integration's newer
    workaround weeks earlier and added **Google Play attestation** to the app;
  - VW US stopped working for third parties on 2026-08-13 and the EU two-way login was pulled
    on 2026-08-18 (per the
    [vwgroup-connect-ha README](https://github.com/its-me-prash/vwgroup-connect-ha)).
  - This is the opposite direction from opening an owner API.
- **[secondary]** As of the last visible activity (#977, 2026-10-24; #30236, July) there is no
  announcement of an owner API, and no press coverage from September or October 2026 reports one.

## 2. Brand apps: is there an API-key / developer / smart-home feature?

Checked the App Store (DE) version histories on 2026-10-28. **[primary]**

- **Volkswagen app**
  ([id1517566572](https://apps.apple.com/de/app/volkswagen/id1517566572)), latest 4.9.0,
  history back to Sept 2025:
  - no mention of API, API key, developer access, smart home or Home Assistant;
  - the only "connect and control" item is VW's own ID. Charger wallbox.
- **myAudi** ([id440464115](https://apps.apple.com/de/app/myaudi/id440464115)), latest 5.9.0,
  history back to Dec 2024: nothing related. The 5.9.0 note is a generic speed and reliability
  update.
- **My CUPRA App** ([id1576693756](https://apps.apple.com/de/app/my-cupra-app/id1576693756)),
  latest 2.23.1:
  - no API or key feature;
  - 2.13.0 (Jan 2026) mentions "new links … seamless integration with external platforms",
    but names no platforms and gives users no key.
- **[inference]** Contrast with Škoda: the MyŠkoda key screen shipped in app 8.16, which is
  visible in the release cycle (ticket 01). No sibling brand app has shipped an equivalent.
- **Note:** The App Store pages render relative dates ("vor 13 Std."). Absolute dates for the
  newest entries are therefore approximate.

## 3. Official developer portals

- **[primary]** GIS Developer Hub (<https://drivesomethinggreater.com/developer-hub>):
  - offers Charging Control, Charging Data, Diagnostic Data, EU Data Act, Mileage, State of
    Health, Vehicle Identity and similar products;
  - registration is via **ONE Business ID**, and onboarding requires signing a **framework
    agreement** and subscribing to a data product
    ([EU Data Act page](https://drivesomethinggreater.com/content/gis/de/en/eu-data-act));
  - there is **no climate or remote-control product** and **no personal or owner tier**.
- **[secondary]** Partners using these APIs include Ohme (Charging Data + Control, 2026-04-28),
  [Smartcar](https://smartcar.com/blog/smartcar-integrates-with-volkswagen-group-info-services-ag-across-europe)
  (2026-04-30, charging start/stop/schedule) and clever-PV. All are commercial aggregators.
- **[primary]** The EU Data Act portal (<https://eu-data-act.drivesomethinggreater.com/>,
  operated by GIS for VW, VW CV, Škoda, Seat, Cupra, Audi, Bentley, MAN and Elli):
  - the owner signs in with their brand ID and **requests a copy** of their data;
  - the portal itself says vehicle functions are managed "via these applications", meaning
    the brand apps;
  - it mentions no API and no keys.
- **[secondary]** On the Data Act portal's format:
  - delivery is ZIP files of JSON, "within 24 h (not always met)" according to heise 11312776;
  - a community HA integration
    ([forum](https://community.home-assistant.io/t/beta-vw-group-eu-data-act-vehicle-data-for-vw-audi-skoda-seat-cupra-bentley-official-portal/1013514))
    reports about 15 min to several hours of latency and **read-only** access, after a
    "continuous data request" is set up.
- **[primary, probed]** `developer.volkswagen.de`, `developer.cupra.com` and
  `developer.cariad.technology` do not resolve. `developer.audi.com` resolves but times out;
  the IP is in a Huawei Cloud range, so it is probably Audi China **[inference]**.

## 4. Sibling hosts to `public.api.connect.skoda-auto.cz`

**[primary, live DNS/HTTP 2026-10-28]**

- **The Škoda host is Škoda's own gateway, not a CARIAD/group service.**
  `public.api.connect.skoda-auto.cz` resolves through
  `CNAME msp-gw-myskoda-live.westeurope.cloudapp.azure.com`, which is the MyŠkoda platform's
  own Azure gateway.
- **None of the analogous names resolve.** Probed:
  - `public.api.connect.{volkswagen.de, volkswagen.com, vw.com, audi.de, audi.com, cupra.com, cupraofficial.com, seat.com, seat.es, porsche.com, cariad.technology, cariad.digital}`
  - `public.api.{cariad.digital, volkswagen.de}`
  - `api.connect.{volkswagen.de, audi.de, cupra.com}`
- **One false positive.** `public.api.audi.de` resolves, but to
  `dlb-prod-external-audi-ag.lb-prod-eu-rt.anypointdns.net`, a MuleSoft Anypoint enterprise
  gateway. It answers 403 on `/`, and every docs path tried (`/v3/api-docs`, `/docs`,
  `/swagger-ui/index.html`, `/openapi.json`) returns 404. It is unrelated to an owner API
  **[inference]**.
- **[inference]** Because the Škoda Public API runs on Škoda's own MSP stack, it is a brand
  initiative. It does not appear to be a group platform that other brands can simply switch on.
  A VW or Audi equivalent would be new work on CARIAD's side, so no sibling host should be
  expected soon.

## 5. Porsche (brief)

- **[secondary]** No official owner or developer API exists. Only reverse-engineered libraries
  are available (e.g. `pyporscheconnectapi`, which states "not officially supported by Porsche").
- Porsche is on a separate stack and is not in GIS's brand list.

## 6. Unofficial routes (status only, not evidence of an official API)

- The `volkswagencarnet` / evcc / Homey `com.vag.core` style app-API clients are broken or in
  an arms race with VW: attestation was added, and workarounds were blocked as of October 2026.
  **[secondary]**
- The Homey "myAudi" app (May 2026) claims to use the "official Cariad API", but it signs in
  with the user's myAudi email and password against the app backend. That is the same class of
  access VW is shutting down. **[secondary / inference]**
- The project rule stands: **do not build on these.**

## 7. Fit with the `/car` connector (decided in 03)

- **No brand slot for VW / Audi / Cupra / Seat can be filled today.** **[inference]**
- If VW later ships something Škoda-like (owner-generated key in the app, a documented REST
  API), it slots into the **brand picker** as another BYO-key brand. That is the cheap path,
  and the reason to keep the upstream seam.
- If VW instead ships an **OAuth / consent flow**, as the Data Hub "data sharing request
  links" hint, the brand picker would need a redirect-style sign-in for that brand. This is
  the whoop pattern; see `/whoop/oauth/callback`.
  - Since one adapter's authorize form can branch per brand, this probably still fits `/car`.
  - A `car-<brand>` adapter is warranted only if the OAuth client has to be registered per
    brand with its own callback and the per-brand callback can't live under `/car`.
  - This needs re-checking when something concrete exists.
- The **Data Act portal** is read-only, delayed by hours and delivered as ZIP files. It can't
  do v1 actions such as climate, and fresh state is the connector's point, so it is not a
  fit. **[inference]**
- **GIS Data Hub / Smartcar** would make missingmcp a commercial onboarded partner. That
  conflicts with the "no monetization, personal-use" stance and is already out of scope in
  the map. It also covers charging only, not climate. **[inference]**

## Watch list (re-check before the next-OEM slice)

- VW / evcc talks: the evcc blog and evcc discussion #30236. A "joint solution" announcement is
  the signal.
- GIS short news: <https://drivesomethinggreater.com/newsroom/>
- "What's New" in the Volkswagen, myAudi and My CUPRA app listings: look for an "API key" or
  "developer" screen like MyŠkoda 8.16's.
