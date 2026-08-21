# Payment Failure Recovery — attempt allocation under a capped retry budget

Submission for the Razorpay AI Builder Internship 2026 — Track 03, AI Revenue Recovery.

**Status:** Phase 1 — C1 event core landed. C2 classifier and C3 allocator not yet built.

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
| `ASSUMPTIONS.md` _(pending)_ | Every parameter, marked ordinal or cardinal, with sources |
| `NOT_BUILT.md` _(pending)_ | Deliberately rejected scope, with reasons |
| `THREAT_MODEL.md` _(pending)_ | What breaks in production that does not break here |

_(pending)_ documents are Phase 5 deliverables and are not yet written.

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

Dedup is on `x-razorpay-event-id` — a header, not a body field. Delivery is
at-least-once and duplicates are expected, so a duplicate is acknowledged 2xx and
logged, never rejected: 24h of non-2xx disables the webhook.

Decisions read authoritative payment state fetched from the API, never the webhook
payload. A payment marked `Failed` can become `Authorized` for up to three days
while Razorpay polls the bank, and every T+1/T+2/T+3 retry lands inside that window.

## License

MIT
