# CLAUDE.md

Project context for agentic coding sessions. Read this before proposing changes.

---

## What this is

A payment-failure recovery layer for Indian payment rails, built as a submission for the
Razorpay AI Builder Internship 2026. Solo build, ~16 days, ~5h/day.

It will be judged by a Razorpay engineering panel via **architecture review and technical
interview**, not by a demo-day audience. Optimise for defensibility under interrogation,
not for visual polish.

---

## The one-line thesis

> Retry volume is capped by regulation. The open problem is not *retry better*, it is
> **how to allocate a fixed attempt budget** across failures with different recovery
> dynamics — and how to prove that allocation works without inventing the evidence.

---

## Prior art — what Razorpay already has

This is the most important section in this file. **Do not propose features that duplicate
these.** Flag it immediately if a change starts drifting toward them.

| Existing | What it does | Boundary |
|---|---|---|
| Optimizer / Smart Router | ML-driven **in-session** routing, fallback to alternate provider on failure | We are **out-of-session** — the customer has already left |
| Subscriptions retry | Fixed T+1 / T+2 / T+3 schedule, cause-blind, then halt | This is our **baseline arm**, reimplemented from their docs, not a strawman |
| Intelligent Retry Engine (beta) | Merchant-configurable retry strategies for recurring debits | Scope check pending — see Open Questions |
| WhatsApp recovery links | Branded recovery links on debit failure | We do not build messaging. Console adapter only. |

**Our two surviving gaps:**
1. Allocation of a capped attempt budget (1 original + 3 retries) rather than retry timing
2. The aggregation point — every shipped product optimises for a single merchant; nobody
   models gateway-level effects, even though network load is the stated rationale for the cap

---

## Architecture

```
Ingest → Normalize → Classify → Allocate → Guard → Execute → Reconcile → Measure
```

- **Ingest** — dedupe by event id, immutable raw store, **refresh authoritative payment
  state before any decision**. `payment.failed` is provisional; a payment can later become
  authorized.
- **Classify** — four classes: `INFRASTRUCTURE`, `LIQUIDITY`, `ATTENTION`, `TERMINAL`.
  Deterministic rules over a lookup table.
- **Allocate** — spends a budget of 4 attempts. Actions: `ATTEMPT_NOW`, `SCHEDULE_AT`,
  `RECOVERY_LINK`, `HOLD`, `SURRENDER`.
- **Guard** — every action passes through. Attempt cap, cooldown, contact budget, risk block,
  order validity, payment-not-already-succeeded, idempotency.
- **Execute** — adapter pattern. `SimulatorExecutor` is primary. `RazorpayExecutor` is a
  demonstrated integration, **not** the backbone.
- **Reconcile** — no recovery is counted without a payment linked to the original order.
- **Measure** — three arms over identical data, swept across parameterized worlds.

### The stated invariant

> **Never create a second payment obligation while the first is in a non-terminal state.**

This is the double-charge property. It is asserted by property-based tests over randomized
adversarial event orderings, not by a handful of hand-written cases.

---

## Hard rules

**Never:**
- Put an LLM in the money-decision path. LLM use is limited to parsing inconsistent error
  descriptions into the schema. The allocator is deterministic.
- Train a model. There is no real labelled data; a model fit to our own generator learns the
  numbers we typed in. This is a correctness argument, not a scope one.
- Build live webhook infrastructure as the spine, a React dashboard, real SMS/WhatsApp, or a
  multi-gateway abstraction.
- Add a cardinal assumption without recording it in `ASSUMPTIONS.md` with a source.
- Silently swallow a guard block. Every refusal is logged with its reason.

**Always:**
- Config-driven. Every limit lives in YAML so the full comparison can be re-run with changed
  parameters in seconds. This is a deliberate feature — the panel will want to poke at it.
- Append-only audit events. Every decision reconstructable end to end.
- Deterministic and seeded. `make reproduce` regenerates every number in the README.
- Small, frequent, legibly-messaged commits.

---

## Ordinal vs cardinal

The policy may depend on **ordinal** facts — "liquidity failures recover better later than
sooner" — which hold in any plausible world.

The policy may **not** depend on **cardinal** facts — "liquidity failures recover at 41% on
day 30" — which are invented.

Cardinal values may appear in the *simulator*, never in the *policy*. Every cardinal value in
the simulator is sampled from a range during the robustness sweep, and is listed in
`ASSUMPTIONS.md` with its source.

If a proposed change makes the policy read a specific probability, stop and flag it.

---

## Do not write these for me

These are the components the panel will interrogate. They must be authored by hand so they
can be defended line by line. Offer review, tests, refactoring and typing — do not offer
implementations.

- The attempt allocator logic (`allocator/`)
- The four-class taxonomy and its mapping rules
- The misclassification cost matrix and confidence thresholds
- `ASSUMPTIONS.md`, `PRIOR_ART.md`, `NOT_BUILT.md`, `THREAT_MODEL.md`

**Delegate freely:** schemas and dataclasses, simulator scaffolding, world-parameterization
plumbing, Hypothesis generators (invariants specified by hand), the Razorpay adapter, report
generation, CLI plumbing, test fixtures, type hints, refactors.

---

## Stack

Python 3.11+. Pytest + Hypothesis. SQLite. Pydantic for schemas. YAML config. Click or Typer
for the CLI. Static HTML report — no frontend framework. Pinned dependencies.

No dependency is added without being asked first.

---

## Open questions — do not assume answers

- Does the Intelligent Retry Engine cover **one-time** payment failures, or only recurring
  debits? Scope depends on this.
- The 1+3 retry cap is documented by Razorpay; primary NPCI verification pending. Until
  verified, phrase as *"as documented by Razorpay"* — never as a regulator citation.
- Is a payment-failure nudge promotional or transactional under the DLT framework? Different
  registration, consent and timing rules follow. Treated as an open question in the repo with
  both implications stated, not guessed.

---

## Definition of done

A change is done when:
1. It has a test
2. Its decisions are visible in the audit trail
3. `make reproduce` still passes
4. Any new assumption is recorded and classified
5. It can be explained out loud without reading the code
