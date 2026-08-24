# Source: `razorpay-revenue-protect.pdf`

- **Retrieved:** 2026-08-24 (filesystem mtime — when the file arrived in this repo, not
  necessarily when it was downloaded or published)
- **Origin URL:** Razorpay blog — "Beyond the Mandate: Why We Built A Recurring Stack with
  Intelligent Revenue-Protect". Exact permalink not captured at download time; **confirm
  before citing**.
- **Publisher:** Razorpay
- **Published:** 12 March 2026 (stated on the page)
- **Provenance:** **mixed, per figure** — see the table below. Razorpay is primary for its
  own product claims and secondary where it cites unattributed industry data.

## Not machine-parsed

10 pages, ~9,100 characters, prose rather than tables. Reading it programmatically needs a
PDF dependency that is not pinned, and the figures are sentences, so they are transcribed
here by hand with the surrounding quote intact.

## Figures present

| Figure | Quote | Provenance |
|---|---|---|
| 120% increase in UPI Autopay mandate setups, 2025 | "In 2025, the market saw a staggering 120% increase in UPI Autopay mandate setups" | Razorpay assertion, no source cited — **secondary** |
| Involuntary churn ≈30% of subscriber attrition | "Industry data suggests that involuntary churn accounts for nearly 30% of subscriber attrition" | explicitly "industry data", unnamed — **secondary** |
| ~30% drop off pre-registration | "Nearly 30% of subscribers drop off before registration is completed" | Razorpay assertion — **secondary** |
| ~20% of subsequent debits fail | "Around 20% of subsequent debits fail due to insufficient balance, bank downtime, or cancelled mandates" | Razorpay assertion — **secondary** |
| ~18% cancel mandates | "close to 18% of active subscribers cancel mandates, often impulsively" | Razorpay assertion — **secondary** |
| 99.99% availability for mandate execution | "This ensures 99.99% availability for mandate execution" | Razorpay's claim about its own stack — **primary for the claim, unverifiable externally** |

## The figure that matters most, and why it cannot be split

> "Around 20% of subsequent debits fail due to **insufficient balance, bank downtime, or
> cancelled mandates**."

One number covering three distinct causes. In this project's taxonomy those are LIQUIDITY,
INFRASTRUCTURE and TERMINAL respectively — and the published figure gives no split between
them. ATTENTION does not appear in the sentence at all.

This is the central obstacle to a sourced failure mix, and it is recorded here rather than
resolved: any four-way split derived from this figure is interpretation, and interpretation
presented as sourced is worse than an honest invention.

## What it does not contain

No per-class breakdown, no time series, no methodology, no sample size, no definition of
"subsequent debits", and no statement of whether the percentages are of mandates, of
attempts, or of merchants.
