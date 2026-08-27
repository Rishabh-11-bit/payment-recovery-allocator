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
customer-visible failure notification. Modified by one further cardinal
parameter in the same block: `fatigue_multiplier` `[1.15, 1.60]`, applied per
notification after the first.

A second modifier, `class_multiplier`, was deleted — it is recorded further down
this file as a deleted parameter rather than an active one. The hazard is now
class-independent by construction, and `World.revocation_hazard` discards the
class argument with a comment saying why.

**It is also rail-conditional.** UPI carries the full hazard; card and e-mandate
carry a small fraction of it, because there is no two-tap in-app cancel gesture
for either — revoking one means contacting the bank. Applying a UPI-shaped
hazard to all three rails overstated exactly the quantity the mandate-survival
argument rests on.

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
hazard **remains the top breaking condition against Arm B in both range sets** —
C loses 30% of worlds nominally where the hazard is below ~0.0155, and 50% under
stress below ~0.0150, against 6% on the other side in each case.

Against Arm A it is no longer the top condition. It still clears the threshold
under stress (40% of 30 worlds below ~0.0215, against 13%), but three conditions
now separate wins from losses more sharply, and `fatigue_multiplier` displaces it
entirely in the nominal set. That is a demotion and it is recorded as one.

Calibration also surfaced conditions the narrower guessed mix had hidden:

| Condition | Loss rate inside | Outside |
|---|---|---|
| `class_mix_INFRASTRUCTURE` above ~0.418 (stress, vs A) | 53% of 19 worlds | 14% of 81 |
| `rail_mix_emandate` above ~0.129 (stress, vs A) | 47% of 19 worlds | 15% of 81 |
| `class_mix_TERMINAL` below ~0.116 (stress, vs A) | 45% of 20 worlds | 15% of 80 |
| `class_mix_TERMINAL` below ~0.287 (nominal, vs A) | 28% of 60 worlds | 8% of 40 |

All are mechanically obvious once visible, and none could appear while the mix
was pinned: where most failures are transient infrastructure faults, the
baseline's blind retries work and C's conservatism costs; where almost nothing is
TERMINAL, C's main advantage — not spending attempts where recovery is impossible
— has little to work on; and where the batch is e-mandate-heavy, the rail-
conditional hazard means there is barely any revocation risk left to protect
against, so C withholds attempts to preserve mandates that were not going to be
revoked anyway.

### C8 finds this parameter to be the breaking point against Arm B, and no longer against Arm A

The robustness sweep (100 sampled worlds per range set, nominal and stress)
searches every swept parameter for the condition that best separates worlds where
Arm C wins from worlds where it loses. **Against Arm B this parameter is the
answer in both range sets. Against Arm A it has been displaced.**

| Range set | Against | Condition | Loss rate inside | Loss rate outside |
|---|---|---|---|---|
| Nominal | B | below ~0.0155 | 30% of 20 worlds | 6% of 80 |
| Stress | B | below ~0.0150 | 50% of 20 worlds | 6% of 80 |
| Stress | A | below ~0.0215 | 40% of 30 worlds | 13% of 70 |
| Nominal | A | — | *does not clear the threshold* | — |

An earlier version of this section said the parameter was the answer "by a wide
margin". Two changes took that away: `class_multiplier`'s deletion removed the
amplification that made the hazard dominate, and making the hazard rail-
conditional shrank its reach to UPI. What replaced it against Arm A is
`fatigue_multiplier` in the nominal set and the mix parameters under stress — see
those entries.

**The demotion does not weaken the honesty of the claim; it moves where the claim
is fragile.** Arm B is the arm that isolates cause-awareness from contact, so the
B comparison is the one carrying the project's primary attribution, and that is
the comparison this parameter still governs.

The mechanism is not subtle, which is why it is worth stating rather than
burying: if repeated failure notifications cost few mandates, then protecting
mandates buys little, and an arm that contacts everyone and spends its whole
attempt budget simply collects more money. Arm C's conservatism is only
justified if the hazard is real.

**So the result depends most heavily on the number the project can defend
least.** That is the first thing a panel should attack, and it should be
volunteered rather than discovered.

Two conditions that were *expected* to break it and still do not appear as
splits: high TERMINAL link conversion, and time-independent liquidity recovery
(`recovery_LIQUIDITY_per_day`, swept down to `0.0` under stress). Both were
plausible and neither separates wins from losses at all. Worth recording, because
predicting a breaking point and then not finding it is evidence too.

Three parameters that were *not* predicted have since cleared the threshold under
stress, and are recorded here so the list stays a record rather than a highlight
reel: `recovery_LIQUIDITY_base` above ~0.165 (32% of 19 worlds vs B, against 11%),
`recovery_ATTENTION_base` above ~0.037 (31% of 59 worlds vs A, against 7%), and
`emission_fidelity` below ~0.823 (27% of 70 worlds vs A, against 7%). Note that
`recovery_LIQUIDITY_base` is the *level* of liquidity recovery, not its time
dependence — the ordinal fact the policy actually reads is `per_day > 0`, and that
one is still unbroken.

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
the argument uses. No result is quoted at a point in the range.

### This is now the top nominal breaking condition against Arm A

It was previously described here as swept alongside `revocation_per_notification`
with nothing further to say about it. That is no longer true, and the change is
worth stating plainly rather than leaving in the sweep output:

| Range set | Against | Condition | Loss rate inside | Loss rate outside |
|---|---|---|---|---|
| Nominal | A | below ~1.218 | **40% of 20 worlds** | 15% of 80 |

Below roughly 1.22, the second and third notifications are barely more corrosive
than the first, so the hazard stops compounding — and the compounding is what
Arm C's withholding is buying. An allocator that spends attempts more slowly than
the baseline is paying a cycle-recovery cost for a protection that a flat fatigue
curve does not deliver.

**This matters more than its position in the table suggests.** The parameter is
unsourced, its range is narrow, and the breaking point at ~1.218 sits just below
the range's own floor of 1.15 — so the sweep is finding the failure at the very
edge of what was configured as plausible. A reader who thinks fatigue is weaker
than `[1.15, 1.60]` assumes is a reader for whom this result does not hold, and
there is no published figure to tell them they are wrong.

It is also the second unsourced cardinal the mandate-survival argument leans on,
alongside `revocation_per_notification`. `class_multiplier` was deleted for being
a third. This one survives deletion pressure because the ordinal claim behind it —
repeated contact compounds — is the mechanism itself rather than a modifier on it.

**Open, same as the parent entry:** whether fatigue is better modelled per
notification or per notification *within a window*. Three failures in three days and
three across three months are not the same experience, and the model does not
distinguish them.

---

## `mandate.class_multiplier` — DELETED

**Status:** removed from `config/worlds.yaml` and from `World`. Recorded here
because deleting a parameter is a result, and a reader who finds it in the git
history should find the reason too.

### What it was

A per-class multiplier on the revocation hazard, encoding that being told
repeatedly you have no money is a worse message to receive than a technical
decline: INFRASTRUCTURE `[0.6, 0.9]`, LIQUIDITY `[1.1, 1.6]`, ATTENTION
`[0.9, 1.2]`, TERMINAL `[0.8, 1.2]`.

### Why it was suspect

It is a claim about customer psychology with **no observable ground truth** —
nothing in any available data records how a customer felt about a notification.
That is the same objection that puts goodwill scoring in `NOT_BUILT.md`. The
defence was that it lived in the simulator, which is permitted cardinal values,
and never in the policy. That defence is real but thin: the mandate-survival
result would still have leaned on an unsourced psychological asymmetry *in
addition to* an unsourced hazard rate.

### The test

Every multiplier set to `1.0` — no asymmetry at all — and the seed-42 crossover
and the full sweep re-run against the version with it.

**This table is a historical record and is deliberately not regenerated.** It
compares two builds, and one of them no longer exists: the parameter is deleted,
so the left-hand column cannot be reproduced without resurrecting it. The figures
are those the two builds produced *at the time of the test*, under the
single-hazard model that preceded the rail-conditional one. They are the evidence
that justified the deletion, and re-running only the surviving column against a
changed model would destroy the comparison rather than refresh it.

| | With multiplier | Flat 1.0 |
|---|---|---|
| C ahead of A @12mo, nominal | 93% | **93%** |
| C ahead of B @12mo, nominal | 97% | **96%** |
| Crossover median vs A | 1.5 mo | 1.6 mo |
| Crossover median vs B | 1.6 mo | 1.7 mo |
| Dominance ordering | `C > A > B`, 0 inversions | **`C > A > B`, 0 inversions** |
| Stress breaking point | revocation hazard | **revocation hazard** |

For the current model the corresponding figures are 80% ahead of A and 89% ahead
of B at 12 months, with crossover medians of 1.8 and 2.8 months. Those are lower
across the board because the hazard is now rail-conditional, not because the
deletion cost anything — the comparison above is what isolates the deletion.

### The result, and why it justified deletion

**The crossover survives with no psychological asymmetry.** Win rates move by at
most a point, the dominance ordering is unchanged with zero inversions, and the
breaking point stays on the revocation hazard.

So the claim never rested on it. A parameter that changes no conclusion but adds
an unsourced assumption is strictly worse than no parameter: it widens the
surface a reviewer can attack while buying nothing. **The result is stronger
without it** — the mandate-survival argument now leans on exactly one unsourced
cardinal (`revocation_per_notification`) rather than two, and that one is swept
and reported only as an ordering.

The hazard is now class-independent by construction, and `World.revocation_hazard`
discards the class argument with a comment saying why.

### What the deletion did change

Two second-order effects, both stated rather than smoothed over:

- **Reported figures moved slightly.** Arm C's seed-42 recovery moved from
  ₹113,942 to ₹108,557 at the time of the deletion, and the seed-42 crossover
  band widened from 0.5–3.2 to 0.6–6.5 months vs B. Both have since moved again
  under the rail-conditional hazard: ₹108,508 and 1.2–9.1 months. The README
  carries the current values.
- **A second breaking condition surfaced under stress vs A**:
  `class_mix_TERMINAL`, which was present before but sat below the reporting
  threshold; with the hazard's effect no longer amplified for LIQUIDITY, it
  clears it. It still clears it today, at below ~0.116 losing 45% of 20 worlds
  against 15% elsewhere. That is the sweep finding something real, not the
  deletion breaking something.

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

Sweeping it is how C8 tests whether the result depends on a clean signal. The
range is unsourced and its lower bound under stress (`0.40`) is chosen to be
implausibly bad rather than realistic.

### It does depend on the signal, below a point — and that is new

This entry previously said sweeping fidelity *demonstrates* the result does not
depend on a clean signal. The current sweep contradicts that:

| Range set | Against | Condition | Loss rate inside | Loss rate outside |
|---|---|---|---|---|
| Stress | A | below ~0.823 | 27% of 70 worlds | 7% of 30 |
| Nominal | — | — | *does not clear the threshold* | — |

Inside the calibrated range (`[0.75, 0.95]`) it still separates nothing, so the
original claim holds where it was actually being made. Under stress it does not.
The mechanism is direct and not a surprise once seen: Arm C is the only arm that
*acts on* the class, so it is the only arm degraded by a payload that misreports
the class. A and B ignore the classification entirely and are therefore immune to
its being wrong.

**That asymmetry is worth volunteering rather than defending.** A cause-aware
allocator's advantage over a cause-blind one is bounded above by how well the
cause can be read, and below roughly 0.82 the reading is poor enough that
knowing-why stops paying for itself. It is the cleanest statement of this
project's own precondition, and the sweep found it rather than the prose
asserting it.

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

### `rail_mix_emandate` is now a breaking condition

| Range set | Against | Condition | Loss rate inside | Loss rate outside |
|---|---|---|---|---|
| Stress | A | above ~0.129 | 47% of 19 worlds | 15% of 81 |
| Stress | B | above ~0.129 | 32% of 19 worlds | 11% of 81 |

This appeared when the revocation hazard became rail-conditional, and it is the
new model becoming visible rather than a new weakness. E-mandate carries almost no
revocation risk — there is no in-app cancel gesture for an e-NACH, revoking one
means contacting the bank — so an e-mandate-heavy batch is one where the
allocator's conservatism has little left to protect. Arm C withholds attempts and
contacts to preserve mandates that were not going to be revoked anyway, and pays
the cycle-recovery cost for nothing.

The single-hazard model could not have surfaced this: it had no way to express a
rail on which revocation is rare. A sweep cannot find a failure mode living in a
distinction the model does not draw.

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

## `ltv.remaining_lifetime_months` — DELETED

**Status:** removed from `config/worlds.yaml` and from `World`. This entry
previously described it as sampled-but-unconsumed and retained anyway; it is
neither sampled nor retained now.

### What it was

A plausible band, `[6, 18]`, for a subscription's remaining lifetime.

### Why it went

It was **sampled and never read**. The reported horizon analysis sweeps lifetime
from 1 to 24 months explicitly and reports the crossover, so nothing consumed the
sampled value. A parameter that is drawn per world and then ignored is worse than
absent: it appears in the config as though it bounds something, it invites a
reader to ask what result depends on it, and the answer is none.

Deleting it was free — no figure moved, because no figure read it.

**The name stays on `contract.FORBIDDEN_ATTRIBUTES`.** Deleting the parameter
removes today's problem; keeping the name forbidden means reintroducing it as a
*policy* input is still caught by the ordinal check rather than discovered later.
A deleted cardinal and a forbidden cardinal are different guarantees and the
second one is the durable one.

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
