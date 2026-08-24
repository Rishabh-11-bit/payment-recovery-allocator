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

## Classifier value space — verified vs documentation-derived

**Classification:** STRUCTURAL — not a magnitude, but an assumption about what the
API emits, and it has already been wrong once
**Location:** `config/classifier.yaml`, `source_space:` block

The `(method, source, step, reason)` value space is **a lower bound, not an
enumeration** — see CHALLENGES 007. What follows records how much of it rests on
evidence and how much on documentation.

### Verified against real test-mode captures

Five payloads in `tests/fixtures/payments/`, captured from test mode:

| method | source | step | reason |
|---|---|---|---|
| `netbanking` | `bank` | `payment_authorization` | `payment_failed` |
| `card` | `business` | `payment_initiation` | `international_transaction_not_allowed` |
| `wallet` | `customer` | `payment_authentication` | `payment_cancelled` |

`netbanking`/`bank` contradicted the reference outright and is in the value space
on this evidence alone. `wallet` is a method the value space does not cover at
all; it is not flagged as anomalous because there is nothing to check against.

### UNVERIFIED — documentation-derived only

**Test mode did not expose UPI.** No UPI mandate failure was captured, so every
UPI entry in the value space and every UPI rule in the taxonomy rests on the
error-parameters reference alone:

- `customer_psp` — unverified
- `network` — unverified
- `beneficiary_bank` — unverified
- the UPI `step` values (`payment_debit_response`, `payment_authentication`,
  `payment_initiation`, `payment_creation`, ~14 in total) — unverified

This matters more than the netbanking correction did. **UPI Autopay is the
project's primary rail** — the failure-rate figures that motivate the whole
build are UPI figures (~8–15% vs ~2–3% for card mandates), and the simulator's
rail mix is UPI-weighted. The rail the argument rests on is the one with no
captured evidence behind its classifier keys.

Given CHALLENGES 007, the prior should be that the UPI documentation is also a
subset of reality, and that at least one UPI source or step will turn out to be
absent from it. The lower-bound handling means such a payload is surfaced rather
than rejected, so the failure mode is now visibility rather than data loss — but
the taxonomy rows themselves would still be wrong.

**Open, and worth stating to the panel rather than hiding:** the UPI mappings are
the least evidenced part of the classifier and the most load-bearing. Capturing a
real UPI Autopay failure is the single highest-value fixture still missing.

### Emandate

Also unverified. No emandate capture. The bare `bank` source is documented for
emandate and was not contradicted, but neither was it confirmed.

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

### Under the calibrated profile

Re-run under `bounded-2026`, which sweeps the LIQUIDITY/INFRASTRUCTURE/TERMINAL
split across the full simplex instead of pinning it near a guessed point, the
hazard **remains the dominant breaking condition under stress** — C loses 40% of
worlds against A and 65% against B where the hazard is below ~0.010.

Calibration also surfaced two conditions the narrower guessed mix had hidden:

| Condition | Loss rate inside | Outside |
|---|---|---|
| `class_mix_INFRASTRUCTURE` above ~0.45 (nominal, vs A) | 26% of 39 worlds | 4% of 161 |
| `class_mix_TERMINAL` below ~0.064 (stress, vs A) | 30% of 20 worlds | 7% of 180 |

Both are mechanically obvious once visible, and neither could appear while the
mix was pinned: where most failures are transient infrastructure faults, the
baseline's blind retries work and C's conservatism costs; where almost nothing is
TERMINAL, C's main advantage — not spending attempts where recovery is impossible
— has little to work on.

### C8 found this parameter to be the result's breaking point

The robustness sweep (300 sampled worlds, nominal and stress range sets) searched
every swept parameter for the condition that best separates worlds where Arm C
wins from worlds where it loses. **This parameter is the answer, by a wide
margin.**

| Range set | Condition | Loss rate inside | Loss rate outside |
|---|---|---|---|
| Nominal, vs A | below ~0.0136 | 30% of 30 worlds | 7% of 270 |
| Stress, vs A | below ~0.0102 | 47% of 30 worlds | 4% of 270 |
| Stress, vs B | below ~0.0102 | 60% of 30 worlds | 2% of 270 |

The mechanism is not subtle, which is why it is worth stating rather than
burying: if repeated failure notifications cost few mandates, then protecting
mandates buys little, and an arm that contacts everyone and spends its whole
attempt budget simply collects more money. Arm C's conservatism is only
justified if the hazard is real.

**So the result depends most heavily on the number the project can defend
least.** That is the first thing a panel should attack, and it should be
volunteered rather than discovered.

Two conditions that were *expected* to break it and did not appear as dominant
splits: high TERMINAL link conversion, and time-independent liquidity recovery
(swept down to `per_day = 0.0` under stress). Both were plausible and neither
separated wins from losses as strongly as the hazard. Worth recording, because
predicting a breaking point and then not finding it is evidence too.

### What would change if this is wrong

Nothing that is reported, unless it is wrong by enough to invert the ordering —
which the sweep would surface rather than hide. That is the point of reporting
the ordering instead of the count.
