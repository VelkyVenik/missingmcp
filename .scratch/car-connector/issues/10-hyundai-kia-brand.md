# 10 — Brand: Hyundai + Kia (Pleos Vehicle Data API)

Type: task
Status: needs-triage
Blocked by: 09

## Goal

Add Hyundai and Kia (Genesis comes for free on the same API) as a read-only brand of the
`/car` connector, on the Pleos Vehicle Data API (EU Data Act, Europe only). Facts:
[assets/08-other-brands-owner-api.md §2](../assets/08-other-brands-owner-api.md).

## Open questions

- Pleos API contract: token endpoint (client-credentials?), vehicle list, status fields,
  rate limits, credential lifetime.
- One brand key (`pleos`, marque from the response) or separate `hyundai` / `kia` picker entries?
- Data is the car's last report, never woken: expose its age in `get_vehicle_status`.

## Done when

- Brand client module + fake Pleos upstream in tests.
- `/car` brand card flips from "Soon" to "Beta"; manual smoke with the friend's Hyundai.
