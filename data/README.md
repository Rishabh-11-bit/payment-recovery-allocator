# Calibration data (C9)

Sources backing the cardinal parameters in `ASSUMPTIONS.md`. **Not blocking C1** — but C9
cannot start until these are here, and re-sourcing them late is how a sweep ends up with
numbers nobody can attribute.

Every file here must be traceable: a claim in `ASSUMPTIONS.md` cites a file in this directory,
and that file records where it came from and when it was retrieved.

## Required

| Source | What it calibrates | Status |
|---|---|---|
| NPCI UPI monthly statistics | Mandate execution volume, month-over-month growth | _(not fetched)_ |
| NPCI Autopay / mandate statistics | Mandates registered vs revoked — the ~20M/month revocation figure | _(not fetched)_ |
| RBI payment system indicators | Cross-check on aggregate volumes; card vs UPI mix | _(not fetched)_ |
| Razorpay published figures | Involuntary churn ≈30% of attrition; ~20% of subsequent debits fail; ~18% cancel mandates | _(not fetched — currently cited from CLAUDE.md secondary sourcing)_ |

## Recording rule

Alongside each data file, a sibling `<name>.source.md`:

```markdown
- **Retrieved:** YYYY-MM-DD
- **URL:**
- **Publisher:**
- **Primary or secondary:**
- **Covers:** <period>
- **Used by:** <which ASSUMPTIONS.md entries>
```

Secondary sourcing is acceptable where the primary is not publicly indexed — the NPCI
circular behind the retry cap is the standing example — but it must be *labelled* secondary,
never cited as though it were the regulator's own text.
