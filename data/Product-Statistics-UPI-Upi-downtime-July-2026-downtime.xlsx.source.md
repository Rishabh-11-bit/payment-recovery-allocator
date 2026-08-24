# Source: `Product-Statistics-UPI-Upi-downtime-July-2026-downtime.xlsx`

- **Retrieved:** 2026-08-24 (filesystem mtime — when the file arrived in this
  repo, not necessarily when it was downloaded or published)
- **Origin URL:** https://www.npci.org.in/what-we-do/upi/product-statistics
- **Publisher:** National Payments Corporation of India (NPCI)
- **Provenance:** **PRIMARY** — published by NPCI directly, not a third party reporting on it

## What the file contains

One sheet, `Product Statistics`, 11 data rows.

| Column | Contents |
|---|---|
| `Sr. No.` | row number |
| `Name of the Member Bank` | member bank name, free text |
| `Incident Count` | count of incidents in the month |
| `Downtime in hours` | total downtime as HH:MM |

**Figures present:** per-bank incident counts and downtime durations for July 2026.
Nothing else. No transaction counts, no failure reasons, no success rates.

## Mechanical summary

- Banks reporting downtime: 11
- Total incidents: 27
- Total downtime: 46.7 hours across all banks
- Downtime as share of the month, per affected bank:
  mean 0.57%, min 0.09%, max 1.88%

Month length used: 31 days (744 hours).

## What it does not contain

No breakdown by failure cause, no mandate or Autopay split, and no distinction
between debit failures and any other UPI traffic. It records that a bank was
down and for how long — not what failed while it was.
