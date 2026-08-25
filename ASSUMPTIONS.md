# ASSUMPTIONS

Every parameter, marked **ordinal** or **cardinal**, with its source.

> **This file is hand-authored and incomplete.** It was seeded with one entry —
> the mandate revocation hazard — because that parameter was introduced by C5
> and the dependency needed recording at the moment it was created rather than
> reconstructed later. The structure below is a starting point, not a decision:
> restructure freely.
>
> **Sections marked [INFERRED] are my reconstruction** rather than something you
> stated. The ranges themselves are read from `config/worlds.yaml`; the reasoning
> attached to them is what needs checking.

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

---

# Simulator parameters

Every range in `config/worlds.yaml`, recorded. Cardinal values live in the
simulator, are sampled per world, and are swept in C8 — **the policy reads none of
them**, and `recovery/contract.py` enforces that statically over `allocator/`.

Ranges are deliberately wide where evidence is thin. A wide unsourced range is
honest; a narrow one is a fabrication with a decimal point.

---

## `recovery.*` — retry success curves

**Classification:** CARDINAL, except the ordinal fact noted below
**Location:** `config/worlds.yaml`, `recovery:`
**Consumed by:** `World.recovery`, read only by the environment when resolving an
attempt. Never by an arm.

| Class | base | per_day | cap |
|---|---|---|---|
| INFRASTRUCTURE | `[0.35, 0.60]` | `[-0.06, -0.02]` | `[0.60, 0.75]` |
| LIQUIDITY | `[0.08, 0.18]` | `[0.03, 0.09]` | `[0.35, 0.55]` |
| ATTENTION | `[0.02, 0.06]` | `[0.00, 0.01]` | `[0.05, 0.12]` |
| TERMINAL | `0.0` | `0.0` | `0.0` |

### Sourcing

**No source for any magnitude.** No published data gives recovery probability by
failure cause and elapsed time; the shape is reasoned, the numbers are chosen.

What *is* defensible without a number:

- **`LIQUIDITY.per_day > 0` is the one ordinal fact the policy depends on** —
  liquidity failures recover better later than sooner. Everything the LIQUIDITY
  cells do rests on the sign, never the magnitude. Stress sweeps it to `0.0`, which
  is the assumption's own breaking point, and C8 found the result survives.
- **`TERMINAL` is identically zero and is not sampled.** P(retry succeeds | expired
  card, cancelled mandate) = 0 is definitional. The loader refuses a config that
  makes it nonzero.
- **`ATTENTION.per_day ≈ 0`** — a retry re-runs an interaction the customer already
  declined. **[INFERRED]**: that the marginal value is near zero rather than merely
  low is my reading of the documented `payment_authentication` step, not a measured
  quantity.

### Why the ranges sit where they do

**[INFERRED — all of the following is reconstruction, check it.]** INFRASTRUCTURE is
highest at base and decays, because a transient fault is most likely already gone and
less likely to still be fixable later. LIQUIDITY is lowest at base and rises, because
the account is empty today. ATTENTION is low and flat. The relative ordering carries
the argument; the absolute values do not.

---

## `link_conversion.*` — recovery-link conversion by class

**Classification:** CARDINAL — no source
**Range:** INFRASTRUCTURE `[0.10, 0.20]`, LIQUIDITY `[0.12, 0.25]`,
ATTENTION `[0.25, 0.45]`, TERMINAL `[0.00, 0.06]`
**Location:** `config/worlds.yaml`, `link_conversion:`

### Sourcing

None. Razorpay describes WhatsApp-delivered branded recovery links as part of the
Intelligent Retry Engine but publishes no conversion figure.

### What the ordering encodes

ATTENTION highest — the customer was reached and did not act, so a link *is* the
intervention. TERMINAL lowest but **deliberately nonzero**: TERMINAL means no *retry*
can recover it, not that nothing can. A customer can enter a new instrument. That
distinction is what makes the TERMINAL cells send an offer rather than surrender
entirely.

### The related parameter that is deliberately absent

A card-change offer almost certainly converts better than a generic link on a dead
card. It is **not modelled** — see `NOT_BUILT.md`. Adding it would be a new invented
cardinal favouring Arm C. Its absence understates Arm C.

---

## `mandate.fatigue_multiplier`

**Classification:** CARDINAL — unsourced extrapolation
**Range:** `[1.15, 1.60]` per notification after the first
**Location:** `config/worlds.yaml`, `mandate:`

Compounds the revocation hazard with each further notification. The ordinal claim —
that the third notification is worse than the first — needs no number and is the part
the argument uses. The magnitude is swept alongside `revocation_per_notification` and
no result is quoted at a point in it.

**Open, same as the parent entry:** whether fatigue is better modelled per
notification or per notification *within a window*. Three failures in three days and
three across three months are not the same experience, and the model does not
distinguish them.

---

## `mandate.class_multiplier`

**Classification:** CARDINAL — no source
**Range:** INFRASTRUCTURE `[0.6, 0.9]`, LIQUIDITY `[1.1, 1.6]`,
ATTENTION `[0.9, 1.2]`, TERMINAL `[0.8, 1.2]`

**[INFERRED]** — the reasoning as I understand it: being told repeatedly that you have
no money is a worse message to receive than a technical decline, so LIQUIDITY carries
a higher revocation multiplier. That is a claim about customer psychology with no
observable ground truth behind it, and it is uncomfortably close to the goodwill
scoring rejected in `NOT_BUILT.md`.

The mitigating difference is that this sits in the *simulator*, which is allowed
cardinal values, and never in the policy. But it is worth knowing that the
mandate-survival result leans on an unsourced psychological asymmetry as well as on an
unsourced hazard rate.

---

## `emission.fidelity`

**Classification:** CARDINAL — no source, and structurally different from the others
**Range:** `[0.75, 0.95]`, stressed to `[0.40, 1.00]`
**Location:** `config/worlds.yaml`, `emission:`

Probability that a synthetic failure's emitted payload carries its true class's
characteristic `(source, step, reason)` rather than another class's.

This is not a fact about the world — it is a knob controlling **how hard the
classifier's job is**. At `1.0` the taxonomy is perfectly separable, the cost matrix
never binds, and the comparison would test nothing. Below that, keys are ambiguous and
misclassification costs matter.

Sweeping it is how C8 demonstrates the result does not depend on a clean signal. The
range is unsourced and its lower bound under stress (`0.40`) is chosen to be
implausibly bad rather than realistic.

---

## `batch.rail_mix`

**Classification:** CARDINAL — weakly sourced, directionally
**Range:** upi `[0.55, 0.75]`, card `[0.15, 0.30]`, emandate `[0.05, 0.15]`

Sourced only in direction: UPI Autopay failure rates run ~8–15% against ~2–3% for card
mandates, so a *failure* batch skews UPI relative to the population of mandates. The
specific proportions are chosen.

**[INFERRED]** — the ~8–15% vs ~2–3% figures are carried in `CLAUDE.md` as calibration
sources, but I have not located a primary citation for them in `data/`. They should
either get a sidecar or be marked secondary.

---

## `batch.amount_paise`

**Classification:** CARDINAL — no source
**Range:** `[9900, 129900]` (₹99 to ₹1,299)

Subscription ticket sizes. Chosen to span a plausible spread of Indian subscription
prices, and deliberately kept **under the ₹15,000 AFA threshold** so no case in the
batch requires per-transaction PIN entry — that is a separate flow and modelling it
would confound the comparison.

The AFA threshold itself is sourced (NPCI). The distribution below it is not.

This parameter also sets `monthly_value`, which the horizon crossover divides by — so
it scales the crossover months. Because it appears in both the cycle term and the
annuity term, **the crossover is less sensitive to it than it looks**, but it is not
independent of it.

**[INFERRED]** — the sensitivity claim in the last paragraph is my reasoning from the
crossover formula; it has not been tested by sweeping amount separately.

---

## `ltv.remaining_lifetime_months`

**Classification:** CARDINAL — no source, and never used to produce a figure
**Range:** `[6, 18]`

Sampled but **not consumed** by the reported horizon analysis, which sweeps lifetime
from 1 to 24 months explicitly and reports the crossover rather than a value at any
lifetime.

Retained because the sweep should have a documented plausible band even though no
result is quoted from it.

**[INFERRED]** — that it is currently unconsumed is from reading the code; if you
intended it to bound the horizon sweep, that wiring does not exist.

---

## `horizon_days` and `batch.size`

**Classification:** STRUCTURAL, not cardinal
**Values:** `horizon_days: 10`, `batch.size: 500`

Simulation extent rather than claims about the world. `horizon_days: 10` is long
enough for a four-execution chain with the Emandate asynchronous delay; `batch.size`
trades noise against runtime and is reduced to 200 in the C8 sweep for speed.

**Worth stating because it bounds a result:** at a 10-day horizon, every case that has
not recovered is halted. A longer horizon would let more cases resolve and would change
the halted/revoked split that the exchange-rate metric reports.

---

## `governor.sourced_multiplier` — the weakest link in the sourcing chain

**Classification:** CARDINAL — **unsourced, and bridging two different quantities**
**Value:** `20`
**Location:** `config/default.yaml`, `governor:`

### What it does

Scales NPCI's published per-bank **outage share** into the **failure share** thresholds
the storm governor uses to classify an issuer as strained or degraded:

```
strained_failure_share = mean_share_per_affected_bank[high] × 20  ->  0.156
degraded_failure_share = per_bank_share[high]              × 20  ->  0.531
```

### Why it is the weakest link

Everything else in `bounded-2026` is either sourced or explicitly swept. This is
neither. And the problem is not just that `20` is invented — it is that **the two
quantities it connects are not the same kind of thing**:

- **Outage share** is *time down*: hours a bank was unavailable ÷ hours in the month.
- **Failure share** is *attempts failed*: failed executions ÷ executions attempted.

Converting one to the other requires knowing how attempts distribute across time
relative to outage windows. If attempts were uniform across the month the multiplier
would be near 1. It is set to 20 on the reasoning that **[INFERRED]** recovery attempts
cluster — they are scheduled T+1 after a batch of failures that an outage caused — so
attempts are heavily over-represented during and just after the outage that produced
them.

That reasoning is plausible and entirely unverified. The clustering factor could be 5
or 50.

### What it does and does not affect

**It does not touch any reported result.** The governor is not in the simulated
comparison path — arms propose to the environment's guard, and the governor is a
separate component. No figure in the README moves if this number is wrong.

What it affects is the governor's *sensitivity*: too low and every issuer looks
degraded and is throttled to five executions per window; too high and none ever does,
making the governor inert.

### What would fix it

Attempts-during-outage-window counts, or any published issuer-level success rate.
Neither is in `data/`. Until then the number is exposed in config with a comment naming
it as the acknowledged fudge, rather than buried in a constant.

---

# Recording rule for anything added later

Every new range in `config/worlds.yaml` needs an entry here before it is used to
produce a figure, marked ordinal or cardinal, with a source or an explicit "no source".
A cardinal parameter with no published source is not disqualifying — but an unlabelled
one is.
