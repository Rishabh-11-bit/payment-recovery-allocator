# Payment Failure Recovery — attempt allocation under a capped retry budget

Submission for the Razorpay AI Builder Internship 2026 — Track 03, AI Revenue Recovery.

**Status:** Phase 0 — prior art audit and domain grounding. No application code yet.

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

## Reproducing results

```
make reproduce
```

Fixed seed. Regenerates every number in this README from scratch.

## License

MIT
