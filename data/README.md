# Calibration data (C9)

Sources backing the cardinal parameters in `ASSUMPTIONS.md`. **Not blocking C1** — but C9
cannot start until these are here, and re-sourcing them late is how a sweep ends up with
numbers nobody can attribute.

Every file here must be traceable: a claim in `ASSUMPTIONS.md` cites a file in this directory,
and that file records where it came from and when it was retrieved.

## Present

| Source | Provenance | What it contains |
|---|---|---|
| NPCI UPI downtime, April–July 2026 (4 files) | PRIMARY | Per-bank incident counts and downtime hours |
| NPCI UPI monthly statistics 2026-27 | PRIMARY | All-UPI volume and value by month. August is a **partial month** (19 days) |
| NPCI UPI uptime, July 2026 | PRIMARY | NPCI switch: 100% uptime, zero incidents |
| Razorpay Revenue-Protect blog (PDF) | mixed, per figure | The ~20% / ~30% / ~18% figures. Not machine-parsed |

Each file has a `<name>.source.md` sidecar recording origin, retrieval date and
provenance. Provenance is per *figure*, not per file — Razorpay is primary for claims
about its own stack and secondary where it cites unattributed industry data.

## Still missing

| Source | What it would calibrate |
|---|---|
| NPCI Autopay / mandate statistics | Mandates registered vs revoked — the ~20M/month figure is still second-hand |
| RBI payment system indicators | Cross-check on aggregate volumes; card vs UPI mix |
| Uptime files for April–June 2026 | Only July is present, so "the NPCI switch was up" is evidenced for one month of four |

## Inventory

```
python -m recovery.calibration
```

Reports what each file contains, the mechanical downtime summary, and the profiles
available. Structure only — it never says what a figure means.

## The four-way split problem

None of these files splits failures along INFRASTRUCTURE / LIQUIDITY / ATTENTION /
TERMINAL. Razorpay's ~20% figure covers three of the four in one number and never
mentions the fourth; the NPCI workbooks give outage hours but no failure counts.

Any four-way mix is therefore interpretation. It belongs in a calibration profile's
`interpretation` field, recorded as inferred — not presented as sourced.

Secondary sourcing is acceptable where the primary is not publicly indexed — the NPCI
circular behind the retry cap is the standing example — but it must be *labelled* secondary,
never cited as though it were the regulator's own text.
