# Payment Failure Recovery — attempt allocation under a capped retry budget

Submission for the Razorpay AI Builder Internship 2026 — Track 03, AI Revenue Recovery.

**Status:** Phase 3 — all twelve components built. C1 event core, C2 classifier, C3 allocator,
C4 guard, C5 simulator and three arms, C6 audit ledger, C7 property invariants, C8 robustness
sweep, C9 calibration, C10 rail actions, C11 storm governor, C12 holdout harness.
The classifier's cost matrix and contact costs are authored.

---

## Thesis

Retry volume for recurring debits is capped (1 original attempt + 3 retries). Under a fixed
ceiling, the open problem is not *retry better* — it is **how to allocate a scarce attempt
budget** across failures with different recovery dynamics, and how to prove that allocation
works without inventing the evidence.

## What this is not

Razorpay already ships in-session dynamic routing (Optimizer / Smart Router), a documented
subscription retry schedule, and an Intelligent Retry Engine for merchant-configurable retry
strategies. This project does not duplicate any of them.

See `PRIOR_ART.md` for the full boundary analysis.

## Documents

| File | Contents |
|---|---|
| `CLAUDE.md` | Architecture, constraints, and hard rules |
| `PRIOR_ART.md` | What exists at Razorpay and where this layer sits |
| `CHALLENGES.md` | Running build log |
| `ASSUMPTIONS.md` | Every parameter, marked ordinal or cardinal, with sources |
| `NOT_BUILT.md` | Deliberately rejected scope, with reasons |
| `THREAT_MODEL.md` | What breaks in production that does not break here |
| `DLT_COMPLIANCE.md` | Open question: is a failure nudge promotional or transactional? |

All four Phase 5 documents are drafted. Passages marked **[INFERRED]** in them are
reconstruction rather than established fact and are pending review.

## Running

```
pip install -r requirements.txt
python -m pytest
python -m recovery.reproduce
```

## Reproducing results

```
python -m recovery.reproduce
```

Recreates the database from scratch and regenerates every claim in this README.
Nothing goes in this README that this command does not reproduce.

**C1 — event core.** Replaying one webhook delivery ten times produces exactly one
case and one decision, with the full sequence visible in the audit trail: one
`webhook.received`, nine `webhook.duplicate_ignored`, one `payment.state_refreshed`,
one `case.opened`, one `decision.recorded`.

**C2 — classifier machinery.** Deterministic lookup over a table loaded from
`config/classifier.yaml`. The module contains no taxonomy: the mapping and the cost
matrix are hand-authored, and the loader refuses a file still marked `status: STUB`
unless a caller opts in explicitly.

An unmapped key is never silently defaulted — it reports `mapped=False`, zero
confidence, and emits `failure.unmapped` with the key that missed. Confidence is an
output: only a HIGH band permits excluding an instrument, because excluding on a
misdiagnosis makes recovery harder. A LOW band discards the predicted class and asks
the cost matrix which class has the lowest worst-case cost of being wrong — the
cheaper error, not the more likely class.

**C5 + C3 — simulator and three arms.** A synthetic mandate-debit batch with a rail
dimension (Card / UPI / Emandate). Every cardinal value is sampled from a range, never
fixed. Ground truth is hidden: arms see only the emitted payload, and emission is
deliberately noisy so misclassification costs bind rather than decorate.

All figures below use the **`bounded-2026`** calibration profile, which is the default.
`--profile uncalibrated` runs the earlier guessed mix for comparison.

### One cycle, world seed 42

Mix drawn: INFRASTRUCTURE 48%, TERMINAL 36%, LIQUIDITY 10%, ATTENTION 6%.

| arm | recovered ₹ | attempts | contacts | wasted attempts | wasted contacts |
|---|---|---|---|---|---|
| A | 153,351.63 | 1,174 | 0 | 597 | 0 |
| B | 167,851.33 | 1,033 | 500 | 565 | 202 |
| C | **113,941.57** | 369 | 308 | **37** | 189 |

A→B contact uplift ₹14,499.70. B→C ₹−53,909.76. **Arm C recovers less in one cycle, and
that is the arm working as designed** — it withholds executions and contacts the other
arms spend. Share of the capped budget spent where recovery was impossible: A 51%,
B 55%, **C 10%**.

Switching from the guessed profile to the calibrated one moved Arm C's figure here from
₹151,735 to ₹113,942. That is not a regression: seed 42 under `bounded-2026` draws
INFRASTRUCTURE at 48% and TERMINAL at 36% — a world where most failures are transient and
retrying blindly works, so C should do badly. **Reporting the worse number under the more
defensible profile is the point.** The single-world figure was always a draw, not a result.

### The claim: horizon crossover

A mandate is an annuity. An arm that recovers less now while keeping more mandates alive
is ahead from some remaining lifetime onward, and that lifetime is the claim.

At seed 42, swept across the hazard range: C overtakes B at **0.5–3.2 months**
of remaining lifetime and A at **0.9–3.4 months**.

Over **300 sampled worlds per range set**, which is the figure that counts:

| | vs A | vs B |
|---|---|---|
| C ahead by 6 months | 89% | 92% |
| C ahead by 12 months | **93%** | **97%** |
| C ahead by 24 months | 95% | 99% |
| Crossover p10 / median / p90 | 0.2 / 1.5 / 7.5 months | 0.3 / 1.6 / 5.2 |
| Ahead from the start | 139 worlds | 7 |
| Never overtakes | 13 worlds | 3 |

Arm B wins cycle recovery in **93% of worlds**. Arm C's case is entirely the horizon, and
that is stated rather than buried.

**Mandate survival is reported as an ordering, never a count** — a count would rest on a
per-notification revocation rate nobody publishes. At seed 42: `C > A > B` at every hazard
in the swept range, zero inversions.

Halted and revoked are different exit doors: halted preserves mandate authority, revoked
destroys it. C buys each avoided revocation with 2.0–4.8 additional halts against A and
1.5–4.2 against B, swept across the hazard range. Whether that is a good trade depends on
manual-recovery rates for halted subscriptions, which are not published.

### What the authored cost matrix changed: nothing, and that is the finding

Authoring the cost matrix moved the LOW-band resolution from ATTENTION to
TERMINAL — under the authored asymmetry, predicting TERMINAL has the lowest
worst-case cost, because mistaking a recoverable failure for TERMINAL surrenders
one payment while the reverse spends a capped execution *and* buys a failure
notification.

**Every reported figure above is identical to the run under the stub matrix.**
Not approximately: the same win rates, the same crossover percentiles, the same
breaking point, the same seed-42 table.

That is the LOW row of the decision table doing exactly what it was designed to
do. All four classes share one action at LOW confidence, so the class the cost
model resolves to never reaches a branch. The cost model changes what the audit
trail *records* about an uncertain case and cannot change what is *done* about
one — which is what "the LOW row is uniform by design" means when it is load
bearing rather than decorative.

The cost matrix still binds at MODERATE and HIGH, where the class does select
the cell. It is only under LOW — the band that exists precisely because the
class is a guess — that its answer is deliberately inert.

### Where it breaks

Under the calibrated profile, **no parameter condition separates wins from losses inside
the calibrated ranges**. Under stress ranges deliberately widened past calibration:

> C loses where `revocation_per_notification` is below ~0.0103 — **40%** of those worlds
> against A, **63%** against B, versus 3–4% elsewhere.

The mechanism is plain: if repeated failure notifications cost few mandates, protecting
mandates buys little and contacting everyone wins. That parameter is the least evidenced
number in the project, so **the result depends most on what can be defended least**. It is
volunteered rather than left to be discovered.

### What calibration exposed — the stronger result

The crossover surviving calibration is reassuring. This is the more interesting finding.

The guessed mix pinned INFRASTRUCTURE at 10–25% and TERMINAL at 10–25%. Two conditions
under which Arm C loses sit **outside those ranges**:

| Condition | Loss rate inside | Outside |
|---|---|---|
| `class_mix_INFRASTRUCTURE` above ~0.45 | 26% of 39 worlds | 4% of 161 |
| `class_mix_TERMINAL` below ~0.064 | 30% of 20 worlds | 7% of 180 |

The old sweep **structurally could not sample either region**, however many worlds it drew.
A sweep confined to a guessed range does not test the guess; it tests everything except the
guess. That is the argument for bounding a parameter rather than assuming it, and it is a
stronger claim than the crossover holding: the calibrated sweep found real failure modes
the guessed one was incapable of finding.

**C9 — calibration.** Sources in `data/`, each with a `.source.md` sidecar recording
origin, retrieval date and provenance **per figure** — Razorpay is primary for claims
about its own stack and secondary where it cites unattributed industry data, and the same
document contains both.

```
python -m recovery.calibration
```

The four-way split is not in the published data. Razorpay's figure covers three classes in
one number — "insufficient balance, bank downtime, or cancelled mandates" — and never
mentions the fourth. So `bounded-2026` does not map it: the split is **swept across the
full simplex** and ATTENTION bounded as a residual, with the profile's `interpretation`
field stating that the split is unavailable and therefore swept rather than assumed. A
profile claiming to be calibrated cannot load without that field.

Issuer outage *is* sourced: four months of NPCI per-bank downtime give a mean of
0.57%–0.78% of the month per affected bank, a per-bank range of 0.08%–2.65%, and 25
distinct banks of which two — Central Bank of India and Punjab National Bank — appear in
every month. It is deliberately **not** used to set the INFRASTRUCTURE share, because
converting outage hours into a share of failures needs transaction volume during outage
windows that no file provides.

**C10 — rail actions.** Executes the shaping the allocator emits, on a recovery Payment
Link. This is the **out-of-session** case and that boundary is the point: Optimizer
already does in-session fallback routing, so nothing here competes with it. This is the
link sent *afterwards*, to a customer who has already gone — where there is no session
to fall back within.

Reorder promotes via `sequence` and removes nothing. Exclusion builds an allowlist
(`show_default_blocks: false`) and is **gated to the HIGH band**, because excluding on a
misdiagnosis leaves the customer without the method they would have used, on a page they
already abandoned once. Reorder costs nothing if wrong; exclusion costs the recovery.

`OFFER_RAIL_MIGRATION` builds an *offer* validated against the documented graph — manual
charging of a domestic card is not supported, so there is no version that executes.

**C11 — storm governor.** Jitter plus a per-issuer admission ceiling. Regulatory basis:
NPCI directs PSPs to initiate executions at moderated TPS and may apply rate limiters.
Scheduling T+1 for every failure in a batch produces exactly that spike, aimed at
whichever issuer caused the batch to fail.

**There is no issuer list, and a test enforces that there never is.** The NPCI data shows
outages rotate — 12 of 25 banks appear in exactly one month — and the second persistent
bank looked fine in April (1.67h) and was five times worse by June (9.40h). A blocklist
built from April's data misses it. The ceiling is a function of observed conditions in a
rolling window, so an issuer that degrades is throttled automatically and one that
recovers is released without anyone editing a file.

Thresholds come from the sourced outage distribution in `bounded-2026`, so "degraded"
means *worse than what NPCI actually published*. Jitter is derived from the case key, not
drawn at random: a retried worker must reschedule to the same slot or the audit trail
stops reconstructing. It only ever moves forward, because moving earlier could cross the
PDN lead time or walk into a peak window.

**C12 — holdout harness.** A routing flag sending a stratified fraction of eligible cases
to the documented baseline on real traffic, with uplift computed from realised outcomes.

**Its value is the claim, not the code.** Every rupee figure here is simulated, and an
uplift measured against a hand-written generator measures the ability to invert it. So
magnitude is not claimed — and "we cannot measure magnitude on synthetic data, here is
the instrument that would measure it on real volume" is only credible if the instrument
exists.

Assignment is a hash of the chain key and experiment name: no stored state, so a replayed
webhook cannot move a case between arms mid-experiment, and assignment survives a restart
because there is nothing to survive. Stratified by rail and failure class, because an
unstratified 10% holdout can draw a TERMINAL-heavy control and make the treatment look
good for reasons unrelated to the policy. Uplift is stratum-weighted rather than pooled,
and the output states in as many words that it is **not** a significance test.

**C6 — audit ledger.** A query surface over the append-only trail, and a decision trace.

```
python -m recovery.explain pay_TSjVZi1gipZs5L
python -m recovery.explain --summary
```

Resolves by case id, order id, or any payment on the chain — whoever asks "why did this
happen" has whichever identifier the complaint arrived with. The trace names the
classification, the decision and its idempotency key, and **every guard block with its
reason**: a case that did nothing shows why, because "no decision" and "a decision the
guard refused" are different facts. The ledger holds no write path, enforced by a test.

**C4 — the guard.** Admission control between Allocate and Execute; every proposal
passes through. Mandate-execution cap, non-peak windows, PDN lead time with the 23:50
cutoff, prior-attempt-resolved for Emandate, contact budget and cooldown, order validity
and expiry, payment-not-already-succeeded, idempotency.

Separate from the allocator on purpose: an allocator that polices itself cannot be
audited against its own rules, and every arm must face identical admission rules or the
comparison measures which arm remembered the regulations. **Every block carries a
reason and is attributable per arm**, so what an arm *tried* stays visible next to what
it was *allowed* to do.

Dedup is on `x-razorpay-event-id` — a header, not a body field. Delivery is
at-least-once and duplicates are expected, so a duplicate is acknowledged 2xx and
logged, never rejected: 24h of non-2xx disables the webhook.

Decisions read authoritative payment state fetched from the API, never the webhook
payload. A payment marked `Failed` can become `Authorized` for up to three days
while Razorpay polls the bank, and every T+1/T+2/T+3 retry lands inside that window.

## License

MIT
