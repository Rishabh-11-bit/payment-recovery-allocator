# Payment Failure Recovery — attempt allocation under a capped retry budget

Submission for the Razorpay AI Builder Internship 2026 — Track 03, AI Revenue Recovery.

**Status:** Phase 1 — C1 event core, C2 classifier machinery, C5 simulator with arms A and B.
The classifier's taxonomy and cost matrix are not yet authored; C3 allocator (arm C) not built.

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

Dedup is on `x-razorpay-event-id` — a header, not a body field. Delivery is
at-least-once and duplicates are expected, so a duplicate is acknowledged 2xx and
logged, never rejected: 24h of non-2xx disables the webhook.

Decisions read authoritative payment state fetched from the API, never the webhook
payload. A payment marked `Failed` can become `Authorized` for up to three days
while Razorpay polls the bank, and every T+1/T+2/T+3 retry lands inside that window.

## License

MIT
