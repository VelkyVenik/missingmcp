# 08 — Other brands: who offers owners an official API?

Type: research
Status: resolved
Blocked by: —

## Question

Besides Škoda (and the VW Group, covered by 07), which car brands give a private owner
official API access to their own car today? Per brand: how the owner gets credentials,
read-only vs. commands, limits/cost, and fit with the `/car` brand picker.

## Answer

Researched 2026-10-28. Full facts with per-claim labels and sources in
[assets/08-other-brands-owner-api.md](../assets/08-other-brands-owner-api.md).

- **Addable:**
  - **BMW + Mini** via **BMW CarData**: owner creates a Client ID in the MyBMW portal;
    read-only (REST + MQTT stream). Vaclav already runs it through the Grok bot.
  - **Hyundai / Kia / Genesis** via the **Pleos Vehicle Data API** (EU Data Act): client ID +
    secret from the Pleos Playground; read-only, cached last-reported state, Europe only.
  - Polestar (Data Portal, read-only): possible later, small base.
- **Parked:** **Tesla** Fleet API is official and can send commands, but it is pay-per-use,
  billed to the app owner. A BYO-app model (user's own $10/month credit) works for read-only;
  signed commands need a per-app domain key. Revisit on demand.
- **Not a fit:** Volvo (re-login every 7 days), Mercedes (B2B/paid), Ford (B2B), Renault,
  Stellantis, Nissan (nothing owner-facing), paid aggregators.
- **Toyota / Lexus (added on request):** no owner API. Only an EU Data Act *request* portal
  (EDA) in the customer portal; every community integration logs in with the MyToyota
  password against the app backend. Not addable; watch the EDA portal.
- **Consequence for `/car`:** the next brands are **read-only**. `operations[]` stays empty for
  them, so only capabilities / status / location tools show. Status needs a data-age field.
- **Re-check 07:** evcc now lists "EU Data Act" entries for VW, Audi and Cupra.

## Decisions (Vaclav, 2026-10-28)

- Order: **BMW + Mini** first ([09](09-bmw-mini-brand.md)), then **Hyundai + Kia**
  ([10](10-hyundai-kia-brand.md)).
- Both are announced as **"Soon"** on `/car` and the homepage card right away (reverses the
  spec's "other brands are left out until they exist").
- **Toyota:** maybe one day, the way [pytoyoda/ha_toyota](https://github.com/pytoyoda/ha_toyota)
  does it (unofficial MyToyota login). No ticket yet, and **not on the website**.
