# ASSUMPTIONS

Every parameter, marked **ordinal** or **cardinal**, with its source.

> **This file is hand-authored and incomplete.** It was seeded with one entry —
> the mandate revocation hazard — because that parameter was introduced by C5
> and the dependency needed recording at the moment it was created rather than
> reconstructed later. The structure below is a starting point, not a decision:
> restructure freely.
>
> Still to record: recovery curves per class, link conversion rates, emission
> fidelity, failure-class mix, rail mix, amount distribution, remaining-lifetime
> range. All are in `config/worlds.yaml` and all are currently unrecorded here.

## How to read this

**Ordinal** — a statement about ordering. "Liquidity failures recover better
later than sooner." The policy may depend on these.

**Cardinal** — a specific magnitude. "41% recover by day 30." The policy may
**never** depend on these. They live in the simulator, are sampled from ranges,
and are swept in C8.

A cardinal parameter with no published source is not disqualifying, but it must
be labelled as such and its range must be wide enough to be honest about the
uncertainty. A narrow unsourced range is a fabrication with a decimal point.

---

## `mandate.revocation_per_notification`

**Classification:** CARDINAL — unsourced extrapolation
**Range:** `[0.010, 0.045]` per customer-visible failure notification
**Location:** `config/worlds.yaml`, `mandate:` block
**Consumed by:** `World.revocation_hazard()` (simulator only — never the policy)

### What it is

The probability that a customer revokes their mandate in response to a single
customer-visible failure notification. Modified by two further cardinal
parameters in the same block: `fatigue_multiplier` `[1.15, 1.60]`, applied per
notification after the first, and `class_multiplier`, which makes repeated
"you have no money" messages more corrosive than technical declines.

### Why it exists

The ordinal claim needs no number: *repeated failure notifications increase the
probability a mandate is revoked.* That is obviously true, it is asserted in
code, and it is tested (`test_revocation_hazard_compounds_with_notifications`).

Producing a **count** of mandates preserved is what needs a number. There is no
way to output a count without modelling revocation, and modelling revocation
requires a per-notification probability. That was the reason to stop reporting
the count.

### How it is used — ordering only, never magnitude

**No result quotes this parameter or any count derived from it.**

`ArmMetrics.mandates_preserved`, `mandates_halted` and `mandates_revoked` exist
but are excluded from `as_row()`. They are reachable only through
`survival_row()`, which documents at the call site that they are hazard-
dependent. `recovery/reproduce.py` prints no mandate count anywhere, and a
reproduce check asserts the reported row contains none.

What is reported is a **dominance ordering**:
`mandate_survival_dominance()` sweeps the hazard across the full configured
range and reports whether one arm preserves more than another at *every* point,
and whether the ordering ever inverts. Endpoints are included deliberately — an
ordering that holds at both extremes of a deliberately wide range holds for any
rate a reader might prefer inside it.

That claim is ordinal. It survives any hazard in the range, so it does not
depend on this parameter's magnitude — only on the parameter being positive and
monotonic in notification count, which is the ordinal fact.

If the ordering ever inverts, **the inversion is the finding** and the crossover
hazard is reported instead of a dominance claim.

### Sourcing

No published per-notification revocation rate was found. The range is
extrapolated from two real figures, neither of which pins it:

- ~20 million AutoPay mandates revoked monthly, mainly insufficient balance
  (NPCI data, via Business Standard)
- 808M mandate executions in July 2025 — a ~2.5% monthly revocation rate against
  executions, but that is revocations per *mandate-month*, not per *failure
  notification*, and the two differ by the failure rate and by how many
  notifications a failing mandate generates
- Razorpay: ~18% of customers cancel mandates; involuntary churn ≈30% of
  attrition

The range is deliberately wide — a factor of 4.5 between endpoints — because
the extrapolation is weak. Narrowing it without a source would be a fabrication.

**Open question for the panel rather than an answer to assert:** is
notification fatigue better modelled per-notification, or per-notification-
within-a-window? A customer who receives three failures in three days plausibly
reacts differently from one who receives three across three months, and the
current model does not distinguish them.

### What would change if this is wrong

Nothing that is reported, unless it is wrong by enough to invert the ordering —
which the sweep would surface rather than hide. That is the point of reporting
the ordering instead of the count.
