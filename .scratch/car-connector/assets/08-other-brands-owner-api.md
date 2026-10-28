# 08 — Non-VW-Group brands: owner API facts (researched 2026-10-28)

Question: besides Škoda, which car brands give a **private owner** official API access to their
own car today, and how would each fit the `/car` brand picker?

Labels: **[primary]** = the vendor's own page; **[secondary]** = community docs (mostly evcc),
press, forums; **[inference]** = my conclusion; **[operator]** = verified by Vaclav himself.

## Verdict

| Brand | Official owner access | How the owner gets credentials | Read / control | Fit with `/car` |
| --- | --- | --- | --- | --- |
| **BMW + Mini** | ✅ BMW CarData (opened to private users for the EU Data Act) | MyBMW web portal → CarData → Technical Access → create Client ID, enable CarData API + CarData Stream | **Read-only** (REST + MQTT stream) | **Next slice.** Owner-generated credential, like the Škoda key |
| **Hyundai / Kia / Genesis** | ✅ Pleos Vehicle Data API (EU Data Act channel) | pleos.ai/playground → "My Vehicle Data API" → pick brand → sign in with Bluelink / Kia Connect / Genesis account → client ID + secret | **Read-only**, last state the car reported, never wakes it; Europe only | **Slice after BMW.** Pure form (client ID + secret) |
| Polestar | ✅ Polestar Data Portal (EU Data Act) | data-portal.polestar.com → client ID, secret, account ID | Read-only | Possible later; small base |
| Tesla | ✅ Fleet API ("official 3rd party API") | Owner must register their own app on developer.tesla.com (OAuth, redirect URI) | Read + commands, but commands need signed requests + a virtual key tied to the app's domain | Parked: pay-per-use, see §3 |
| Volvo | ✅ developer.volvocars.com | Operator registers an app; owner consents with Volvo ID; full access approved manually (14–21 days per FAQ) | Read (scopes evcc uses) | Poor: re-login every 7 days |
| Mercedes-Benz | ❌ B2B / paid; community uses tokens scraped from the app | — | — | No |
| Ford | ❌ FordConnect is B2B (evcc: "FordConnect Query") | — | — | No |
| Renault, Stellantis (Peugeot, Opel, Citroën, DS, Fiat), Toyota, Nissan | ❌ nothing owner-facing; Renault only fleet APIs | — | — | No |
| Smartcar / High Mobility / Enode | Paid B2B aggregators | — | — | No (no-monetization stance) |

## 1. BMW + Mini — CarData

- **[secondary]** Client ID is created once per BMW account in the MyBMW portal (Vehicle overview
  → BMW CarData → Technical Access), then "CarData API" and "CarData Stream" are switched on
  (Loxone / thermondo guides, ioBroker wiki).
- **[secondary]** Live data is pushed over MQTT 3.1.1/TLS to
  `customer.streaming-cardata.bmwgroup.com:9000`, one topic per VIN; the owner must select the
  data points to stream (~244 available), otherwise the stream stays silent.
- **[secondary]** Reported limits: one integration can receive the stream at a time; a daily
  quota on REST calls. Not confirmed on a BMW page.
- **[secondary]** bimmer_connected #745: CarData was opened to private users without the
  earlier sign-up and fees, to comply with the EU Data Act.
- **[secondary]** evcc `bmw-cardata`: needs only `clientid`; data arrives "on restart and when
  received using streaming … only when the vehicle actively creates new updates". No commands.
- **[operator]** Vaclav runs CarData for his own car through the Grok bot and it works well —
  the sign-in flow and limits can be read off that setup.
- **Open before implementation:** exact OAuth flow (I believe device-code + PKCE, unverified),
  token lifetimes, the real REST quota, and whether a hosted gateway can hold the MQTT stream
  per account or must poll REST instead (the 20/h Škoda-style budget logic may not map 1:1).

## 2. Hyundai / Kia / Genesis — Pleos

- **[primary]** Hyundai's Data Act notice: third parties integrate with the Vehicle Data API,
  then the owner approves sharing in the third party's app; the API serves status + basic data
  for Hyundai, Kia and Genesis vehicles whose owners consented.
- **[secondary]** evcc `hyundai-eu-data-act` / `kia-eu-data-act`: register at the Pleos
  Playground, "My Vehicle Data API", select the manufacturer, sign in with the main user's
  Bluelink / Kia Connect / Genesis account → `clientid` + `clientsecret`. "The API returns the
  data last reported by the vehicle to the manufacturer's servers; it does not wake the
  vehicle." Default cache 15 min. Europe only. `vin` needed only with several cars.
- **[secondary]** fhempy PR #716 switched its Kia/Hyundai integration to the same Pleos API.
- **Fit:** form fields client ID + secret (+ VIN), one brand entry covering three marques.
  `credential_subject` = brand + VIN works as for Škoda. Status must expose data age, since
  values can be stale.
- **Tester:** a friend of Vaclav's owns a Hyundai — the manual release gate.

## 3. Tesla — Fleet API

- **[primary]** "Fleet API is Tesla's official 3rd party API and the only supported API for
  vehicle interactions" (changelog 2024-03-26).
- **[primary]** Pay-per-use from 2025-01-01, billed to the **app owner** (changelog 2024-11-27).
- **[secondary]** Each developer account gets a $10/month discount, roughly data streaming +
  100 commands + 2 wakes/day for two cars; commands ≈ $1 per 1,000 (Teslemetry, Tesla dev page
  snippet). Rates not re-checked on Tesla's pricing page.
- **[primary]** Before a command, the app's virtual key must be on the car; a new refresh token
  invalidates the previous one (2024-02-01).
- **[inference]** One missingmcp app for everyone = the operator pays per user → conflicts with
  no-monetization. BYO app (each user registers their own, pays from their own $10 credit) works
  for **read-only**; commands need the app's public key hosted on the app's domain, which a
  shared missingmcp.com likely can't do per user. Parked until there's demand; then verify
  pricing and read-only without a domain.

## 4. Volvo, Mercedes, others

- **[primary]** Volvo: developer portal, VCC API key per app, owner consent via Volvo ID; full
  credentials granted manually (FAQ, possibly dated). **[secondary]** evcc: "Volvo enforces
  re-login every 7 days" → a Claude connector would break weekly.
- **[secondary]** Mercedes: developer APIs aimed at businesses, mostly paid; evcc and
  mbapi2020 use access/refresh tokens taken from the app login. Not an owner API.
- **[secondary]** evcc index (2026-10-28) has no EU Data Act / official entry for Renault,
  Stellantis brands, Toyota, Nissan, Mercedes, Porsche, Smart; Ford only FordConnect Query.

## 5. Re-check for ticket 07

- **[secondary]** The evcc index now lists **"EU Data Act"** entries for **Volkswagen, Audi and
  Cupra**. Ticket 07 judged the GIS Data Act portal as delayed ZIP exports. Worth a look whether
  a continuous, API-style Data Act channel exists for VW Group now (read-only at best).

## Sources

- evcc: [vehicles index](https://docs.evcc.io/en/vehicles/),
  [BMW CarData](https://docs.evcc.io/en/vehicles/bmw-cardata.md),
  [Hyundai EU Data Act](https://docs.evcc.io/en/vehicles/hyundai-eu-data-act.md),
  [Kia EU Data Act](https://docs.evcc.io/en/vehicles/kia-eu-data-act.md),
  [Polestar](https://docs.evcc.io/en/vehicles/polestar.md),
  [Volvo](https://docs.evcc.io/en/vehicles/volvo.md),
  [Mercedes-Benz](https://docs.evcc.io/de/vehicles/mercedes-benz.md)
- [bimmer_connected #745](https://github.com/bimmerconnected/bimmer_connected/discussions/745),
  [HA: BMW CarData via MQTT](https://community.home-assistant.io/t/bmw-cardata-directly-via-mqtt/999864)
- [Hyundai data rights](https://connected-mobility.hyundai.com/data-rights-en),
  [fhempy PR #716](https://github.com/fhempy/fhempy/pull/716)
- [Polestar data notice](https://www.polestar.com/global/legal/data-compliance/data-notice/)
- [Tesla Fleet API announcements](https://developer.tesla.com/docs/fleet-api/announcements),
  [Teslemetry: pay per use](https://teslemetry.com/blog/tesla-fleet-api-pay-per-use)
- [Volvo: getting started](https://developer.volvocars.com/apis/docs/getting-started)
- [mbapi2020](https://github.com/renenulschde/mbapi2020)
