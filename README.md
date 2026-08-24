# Payment Failure Recovery — attempt allocation under a capped retry budget

Submission for the Razorpay AI Builder Internship 2026 — Track 03, AI Revenue Recovery.

**Status:** Phase 2 — C1 event core, C2 classifier (taxonomy authored), C3 allocator,
C4 guard, C5 simulator with all three arms, C7 property-based invariant tests, C8 robustness
sweep. Cost values still stubbed.

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
| `ASSUMPTIONS.md` _(seeded)_ | Every parameter, marked ordinal or cardinal, with sources |
| `NOT_BUILT.md` _(pending)_ | Deliberately rejected scope, with reasons |
| `THREAT_MODEL.md` _(pending)_ | What breaks in production that does not break here |

_(pending)_ documents are Phase 5 deliverables and are not yet written. `ASSUMPTIONS.md`
is _(seeded)_: it holds one entry, for the mandate revocation hazard introduced by C5.

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

**C5 — simulator, arms A and B.** A synthetic mandate-debit batch with a rail dimension
(Card / UPI / Emandate). Every cardinal value is sampled from a range in
`config/worlds.yaml`, never fixed — C8's sweep is many worlds, so parameterisation is
structural rather than retrofitted.

Ground truth is hidden: arms see only the emitted payload, and emission is deliberately
noisy so misclassification costs bind rather than decorate. The environment — not the
arms — enforces the attempt cap, the non-peak windows and the PDN lead time, so no arm
can benefit from ignoring them, and every rejection is counted and attributed.

Arm A is Razorpay's documented schedule, reimplemented and cause-blind: Card and UPI at
T+1/T+2/T+3 then halted, Emandate asynchronous with bank-holiday shifting. Arm B adds one
generic recovery link per failure, so A→B isolates the value of contact and B→C will
isolate the value of cause-awareness.

**Mandate survival is reported as a dominance ordering, never as a count.** A count
depends on a per-notification revocation rate nobody publishes. The ordering does not:
the hazard is swept across its full configured range, and what is reported is whether
one arm preserves more than another at every point, and whether the ordering inverts.
Same discipline as LTV — swept, never quoted at a point. See `ASSUMPTIONS.md`.

**The figures `reproduce` prints for C5 are a single world draw and are not a result.**
A defensible number needs C8's sweep across sampled worlds and its stated breaking point.

**C7 — property-based invariants.** The safety invariant is *never create a payment
obligation outside the original order's attempt chain while that chain is within its
late-authorisation window.* Orderings are generated rather than hand-written —
duplicate deliveries, out-of-order deliveries, failed-then-late-authorized inside the
3-day window, a worker crashing between claim and finish, two workers on one case,
order expiry mid-recovery, a PDN window shift.

```
python -m recovery.reproduce --c7-sequences 5000
```

**Verified: 5,000 adversarial orderings, 42,715 events, no violation** — re-run after
C4 so the result covers every generated hazard rather than a subset. The search is
seeded (`seed=20260823`), so that run reproduces exactly; it takes ~12 minutes.
`reproduce` defaults to 500 orderings to stay quick, and always prints the count it
actually explored — a clean run is worth only the size of the search.

The orderings are sampled from the generated space, not enumerated over it, so this is
evidence rather than proof. Hypothesis searches the same space adaptively in the test
suite and shrinks any failure to a minimal sequence.

**The search is validated by mutation**, which is what licenses the claim. Four planted
bugs, each of which the search must find: the late-authorisation guard removed, the
attempt chain split, the order-expiry check disabled, the execution timing checks
disabled. A search that cannot find a planted bug is not evidence of absence.

A separate test asserts every hazard actually fires a block across several seeds — a
hazard that never blocks anything is a hazard in name only, and that check is what
caught the PDN window being masked by the peak-hour check.

**C8 — robustness sweep.** Every cardinal value is redrawn per world — recovery
curves, link conversion, revocation hazard, failure mix, rail mix, emission fidelity
— and all three arms run in each. Two range sets: `nominal` samples inside the
calibrated ranges, `stress` widens them past calibration to locate the edge.

```
python -m recovery.reproduce --sweep-worlds 300
```

The output is the **breaking point**, not a win rate. Verified over 300 worlds per
range set:

| | vs A | vs B |
|---|---|---|
| C ahead by 12 months | 91% of worlds | 96% |
| Crossover, p10 / median / p90 | 0.2 / 1.2 / 5.2 months | 0.3 / 1.5 / 5.2 |
| Never overtakes | 25 worlds | 7 |

**C loses where the revocation hazard is low** — below ~0.010–0.014 per
notification, C loses 30–60% of those worlds against 2–7% elsewhere. The mechanism
is plain: if repeated failure notifications cost few mandates, protecting mandates
buys little and contacting everyone wins. That parameter is the least evidenced
number in the project, so **the result depends most on what can be defended least**.
See `ASSUMPTIONS.md`.

Arm B wins cycle recovery in ~93% of worlds. Arm C's case is entirely the horizon.

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
