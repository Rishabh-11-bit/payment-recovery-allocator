# CLAUDE.md

Project context for agentic coding sessions. Read fully before proposing changes.

---

## What this is

A mandate retry sequencer for UPI Autopay and card subscriptions — a system that decides how
to spend a regulator-capped budget of payment attempts across failures with different recovery
characteristics.

Submission for the **Razorpay AI Builder Internship 2026, Track 03 (AI Revenue Recovery)**.
Solo build, ~15 days, ~5h/day. Judged by a Razorpay engineering panel via **architecture
review and technical interview**. Optimise for defensibility under interrogation.

## The bar (this is the rubric — there is no other)

> Don't just identify the problem. Show **measured money recovered across a batch**, with
> **compliant escalation**, **stopping rules**, and an **audit trail**.

All four are required. In particular, the batch money figure must be produced — not refused.
What we control is how precisely it is characterised.

## Thesis

> Retry volume is capped at four attempts by NPCI. Execution is barred from peak hours and
> requires 24h advance notice. There is almost no timing freedom left — so the highest-value
> decision is **not spending an attempt on a failure that cannot recover.** The documented
> baseline spends three of four attempts on expired cards and cancelled mandates.

## The objective function — read this before touching the allocator

**The scarce resource is the mandate, not the attempt.** Attempts are the currency spent
against it.

A failed debit costs one transaction. A revoked mandate costs the subscriber's entire
remaining lifetime. Razorpay's own figures: ~20M mandates revoked monthly (mainly insufficient
balance), involuntary churn ≈30% of all attrition. Their entire Revenue-Protect positioning is
churn, not transactions.

Consequences for the allocator:

- Every attempt carries a cost in **mandate-survival probability**, not just retry cost.
  Three failure notifications in three days to a customer who is broke is a good way to get the
  mandate cancelled from their UPI app.
- `SURRENDER` is not "give up on this money." It is **"protect the annuity"** — a positive
  action with a defensible value. This is what makes the stopping rules productive rather than
  merely defensive.
- `halted` is the state we are actually avoiding — halted subscriptions need manual
  intervention to recover.
- Escalation threshold is principled: escalate when mandate risk outweighs recovery value,
  not at an arbitrary attempt count.

**This is ordinal, not cardinal.** "Repeated failure notifications increase revocation
probability" is obviously true and needs no invented hazard rate. Do not let a change
introduce one.

### Reporting rules for this — strict

The bar requires **money recovered across a batch**. That stays the headline figure. Mandate
survival is **additive, never a replacement**. Do not demote the rupee figure.

- **Mandates preserved → report as a count.** Definitional and observable.
- **LTV → report as a sensitivity only, never a point estimate.** "At 6 months average
  remaining lifetime, ₹X; at 12 months, ₹Y." Sweep remaining-lifetime in the robustness
  harness like any other cardinal parameter.

A single headline LTV number is a cardinal claim dressed as a result. It would forfeit the
credibility the entire project rests on. If a change proposes one, stop and flag it.

---

## Prior art — do not duplicate

| Existing | What it does | Our boundary |
|---|---|---|
| Optimizer / Smart Router | Routes across gateways/aggregators **in-session**. Params: channel, method, BIN, card type, brand, issuer, bank, amount. **Failure reason is not a routing parameter.** On-demand feature, requires support request. | Different axis: they pick *which gateway*; we decide *whether to spend an attempt* given why the last one failed |
| Subscriptions retry | Fixed schedule, cause-blind (see below) | This is **Arm A**, reimplemented from their docs. Not a strawman |
| Intelligent Retry Engine (beta, FTX 2026) | Merchant-**configurable** retry strategies + templates. WhatsApp branded recovery links. Recurring-only | It is a configuration surface, not a decision engine. We are what would sit behind an "auto" setting. Complementary, not competing |
| Priority-based routing | Creates temporary 20-min downtimes when a gateway's SR drops, routes to next priority | Gateway *selection*. Ours is retry *admission control*. State the distinction explicitly |

---

## Regulatory constraints — these define the action space

NPCI guidelines effective 1 Aug 2025 (press release 21 May 2025; accessed via secondary
sources, primary circular not publicly indexed — cite as such).

- **Attempt cap:** 1 initial execution + up to 3 retries per mandate, by sequence number. Four
  total, ever.
- **Peak-hour ban:** Autopay executions must occur in **non-peak hours only**. Peak is
  **10:00–13:00 and 17:00–21:30 IST**.
- **Moderated TPS:** PSPs directed to initiate executions at moderated TPS; NPCI may apply
  rate limiters to avoid spikes. *This is the regulatory basis for the storm governor — not a
  speculative feature.*
- **Pre-debit notification:** customer must receive SMS/app notification ≥24h before each
  recurring charge, with exact amount and cancel option. **If the PDN fails, the debit fails —
  it is a prerequisite.** PDN requests at/after 23:50 are rejected when debit_date = T+1.
- **AFA:** transactions over ₹15,000 require PIN entry each time.
- Non-compliance: UPI API access restrictions, penalties, onboarding suspension.

**Consequence: `ATTEMPT_NOW` does not exist for mandate debits.** Every attempt is decided
≥24h ahead and must land in a non-peak window. Do not propose sub-daily retry timing.

*Open:* `auto_represent_on_failure` on the PDN reportedly allows automatic re-presentation of
technically-declined presentations — possibly without a fresh PDN. If true, INFRASTRUCTURE
failures have timing freedom LIQUIDITY ones don't. Unverified.

---

## The three arms

The comparison is three-armed. Arm B exists specifically to separate *"contacting people
helps"* from *"cause-awareness helps"* — without it the uplift claim is ambiguous.

| Arm | What it is | Cause-aware | Budget-aware |
|---|---|---|---|
| **A** | Documented baseline — Razorpay's rail-parameterised subscription retry schedule | No | No |
| **B** | Generic recovery — one recovery link to every failure, no instrument shaping | No | No |
| **C** | The allocator — cause-aware, budget-aware, mandate-survival-weighted | Yes | Yes |

**Arm A — documented baseline.** Reimplemented from Razorpay's subscriptions documentation,
**not invented as a strawman**. Cite the docs page in-code at the point of implementation.

| Rail | Behaviour |
|---|---|
| Card | T+1, T+2, T+3 daily, then `halted` |
| UPI | T+1, T+2, T+3 daily, then `halted` |
| Emandate | Async — retry only on confirmation/rejection of prior attempt, can exceed 24h. Charge day shifts for bank holidays: T → T−1; if both T and T−1 are holidays, T → T−3 |

**The retry model never references the failure reason.** Documented failure causes are expired
card, bank-blocked card, insufficient balance, cancelled mandate — and all four get identical
treatment. This is the source of the primary claim.

**Arm B — generic recovery.** One recovery link to every failure. No cause awareness, no
instrument shaping, no budget reasoning. This is the "just contact everyone" arm. Its purpose
is attribution: A→C uplift conflates the value of contact with the value of cause-awareness,
and only B→C isolates the second.

**Arm C — the allocator.** Cause-aware, budget-aware, mandate-survival-weighted. The subject
of the evaluation.

## Rail migration is a directed graph

| From | → Card | → UPI | → Emandate |
|---|---|---|---|
| Card | Yes | Yes | Yes |
| UPI | Yes | No | No |
| Emandate | Yes | No | No |

`OFFER_RAIL_MIGRATION` must validate against this. **Manual charging of a domestic card is
not supported** — card re-attempts are customer-mediated (hosted page / card change), never
programmatic.

**The system offers; the customer acts.** Mandate-level migration cannot be executed
programmatically, so the action is an *offer*, not a switch. Do not model it as one.
This is distinct from link-level shaping (`REORDER_RAILS`, `EXCLUDE_INSTRUMENT`), which the
system *does* execute directly on a recovery Payment Link via `options.checkout`:

| Level | Actions | Who executes |
|---|---|---|
| **Mandate** — move the subscription to another rail | `OFFER_RAIL_MIGRATION` | Customer, via hosted page / card change. Validates against the graph above |
| **Link** — shape the checkout on a recovery Payment Link | `REORDER_RAILS`, `EXCLUDE_INSTRUMENT` | System, directly, via `options.checkout` |

Conflating the two was the error recorded in `CHALLENGES.md` 004.

*Open, not asserted:* Razorpay does not document why UPI cannot migrate to UPI. Likely because
the target rail needs re-authorisation and only cards re-authorise synchronously in-session.
Recorded as a question, not a claim.

---

## Classifier — derived from documented fields, not invented

Key is **`(method, source, step, reason)`** — the value space is method-partitioned.

| Method | `source` values |
|---|---|
| Cards | `customer`, `business`, `internal`, `gateway`, `issuer_bank` |
| UPI | + `customer_psp`, `network`, `beneficiary_bank` |
| Netbanking | `customer`, `business`, `internal`, `issuer_bank` |
| Emandate | `customer`, `bank`, `business`, `internal`, `gateway`, `issuer_bank` |

There is **no `razorpay` source** (it is `internal`) and no bare `bank` except for Emandate.

`step` localises better than `source`. UPI exposes ~14 steps. Notably:

| step | Meaning | Class |
|---|---|---|
| `payment_debit_response` | Customer's bank declined | LIQUIDITY or INFRASTRUCTURE by `source` |
| `payment_authentication` | Customer reached, never entered M-PIN | ATTENTION — retry has ~zero marginal value |
| `payment_initiation` / `payment_creation` | Broke before reaching customer | INFRASTRUCTURE |
| `mandate_creation` | Mandate never registered | Registration failure, distinct from debit failure |
| `card_enrollment_check` | Card not 3DS-enrolled | TERMINAL-adjacent |

Four classes: `INFRASTRUCTURE`, `LIQUIDITY`, `ATTENTION`, `TERMINAL`. Deterministic rules over
a lookup table. LLM only for parsing free-text `description` into schema — never in the
decision path.

**Misclassification costs are asymmetric.** INFRASTRUCTURE→TERMINAL surrenders a recoverable
payment. TERMINAL→INFRASTRUCTURE burns capped attempts and risks escalating a risk flag.
Tune the operating point to a cost matrix, not accuracy. Low confidence falls toward the
cheaper error.

*Note:* UPI Collect deprecated 28 Feb 2026 — **but UPI Mandates (execute/modify/revoke) are
exempt**, so recurring scope survives. Intent-flow `step` distributions differ from Collect.

---

## Safety — the invariant

> **Never create a payment obligation outside the original order's attempt chain while that
> chain is within its late-authorisation window.**

Late auth is documented: no bank response → `Created` for 10 min → `Failed` on timeout →
**Razorpay polls the bank for 3 days** → may become `Authorized`. Under 0.5% of payments;
uncaptured late-auth payments auto-refund in 5 days.

**Every T+1/T+2/T+3 retry lands inside that 72h window.**

Mitigation is architectural, not lock-based: **the Orders API clubs multiple attempts against
the same order.** If one succeeds and another late-authorises, the late one is refunded
immediately and only the successful payment is marked against the order. Therefore **reuse the
original `order_id`**; recovery Payment Links carry `reference_id` linking back to it.

### Webhook semantics (documented — drives C1 and C7)

At-least-once delivery. Duplicates expected. **`x-razorpay-event-id` is the dedup key.**
Non-2xx → exponential backoff for 24h → webhook disabled. **Events may arrive out of order.**
Response slower than 5s is treated as timeout and resent → acknowledge fast, decide in a worker.

---

## Architecture

```
Ingest → Normalize → Classify → Allocate → Guard → Execute → Reconcile → Measure
```

### Component map

Component IDs are referenced throughout this file and in `CHALLENGES.md`.

| ID | Component |
|---|---|
| C1 | Event core — ingest, dedupe, immutable store, state refresh |
| C2 | Failure classifier — four classes, method-partitioned key |
| C3 | Attempt allocator |
| C4 | Guard — caps, windows, idempotency, storm governor |
| C5 | Simulator + three arms |
| C6 | Audit ledger |
| C7 | Property-based invariant tests |
| C8 | Robustness sweep across sampled worlds |
| C9 | External calibration |
| C10 | Rail actions — reorder and instrument exclusion |
| C11 | Storm governor — jitter + per-issuer admission ceiling |
| C12 | Holdout harness |

**Allocate** (not "policy engine"): spends a budget of 4 across a window.
Actions: `SCHEDULE_AT(t)`, `RECOVERY_LINK`, `OFFER_RAIL_MIGRATION`, `REORDER_RAILS`,
`EXCLUDE_INSTRUMENT`, `HOLD`, `SURRENDER`. No `ATTEMPT_NOW` — see PDN constraint.
`OFFER_RAIL_MIGRATION` is mandate-level and customer-mediated; `REORDER_RAILS` and
`EXCLUDE_INSTRUMENT` are link-level and system-executed. See the rail-migration section.

**Guard**: attempt cap, non-peak window check, PDN lead-time check, cooldown, contact budget,
risk block, order validity, payment-not-already-succeeded, idempotency key
`recovery:{payment_id}:{policy_version}:{attempt_n}`, storm governor (jitter + per-issuer
admission ceiling).

**Execute**: adapter pattern. `SimulatorExecutor` primary; `RazorpayExecutor` demonstrated only.

### Rail actions are graduated by confidence

Payment Links support `options.checkout.method` (coarse on/off) and
`options.checkout.config.display.blocks` (instrument-level — remove a specific bank, restrict
by issuer/BIN/card type), plus `sequence` for ordering and
`preferences.show_default_blocks: false` for allowlist construction.

- **High confidence** → exclude the specific degraded instrument (not the whole method)
- **Moderate confidence** → reorder only. Promotes the likely rail, removes nothing
- Excluding on a misdiagnosis makes recovery *harder*. Reorder is the default; exclusion is
  the high-confidence case.

---

## Claim structure

| Tier | Claim | Rests on |
|---|---|---|
| Required | Money recovered across the batch | Simulated, three arms, swept across sampled worlds, with a holdout harness for real traffic. **The bar demands this — never omit it** |
| Primary | Attempts and contacts saved on structurally-unrecoverable failures | **Definitional** — P(retry succeeds \| expired card / cancelled mandate) = 0 |
| Primary | Mandates preserved (count) | Ordinal — repeated failure contact raises revocation probability |
| Secondary | Better placement of surviving attempts | Ordinal only. **Deliberately weak** — regulation leaves little timing freedom, and we say so |
| Sensitivity | Lifetime value protected | **Never a point estimate.** Swept over remaining-lifetime assumptions |

Report the batch figure first because the bar asks for it, then show that a larger quantity —
the mandate — was being protected at the same time. Always alongside the robustness sweep and
its stated breaking point.

## Ordinal vs cardinal

Policy may depend on **ordinal** facts ("liquidity recovers better later than sooner").
Policy may **not** depend on **cardinal** facts ("41% on day 30").

Cardinal values live in the *simulator*, never the *policy*, are sampled from ranges during the
robustness sweep, and are listed in `ASSUMPTIONS.md` with sources. If a change makes the policy
read a specific probability, stop and flag it.

### Calibration sources (real, cite them)

- ~20 million AutoPay mandates revoked monthly, mainly insufficient balance (NPCI data via
  Business Standard)
- 808M mandate executions July 2025, up from 392M YoY; 50M new mandates registered
- UPI Autopay failure rates ~8–15% vs ~2–3% for card mandates
- Razorpay: involuntary churn ≈30% of attrition; ~30% drop off pre-registration; ~20% of
  subsequent debits fail; ~18% cancel mandates

---

## Scope discipline

15 days, solo, ~5h/day. ~91h of work against 64–96h available. No slack.

**Tier 1 — no submission without these:** C1 event core, C2 classifier,
C3 allocator, C4 guard, C5 simulator + three arms, C6 audit ledger.

**Tier 2 — this is what wins:** C7 property tests, C8 robustness sweep,
C9 calibration, C10 rail actions, C11 storm governor, C12 holdout harness.

**Cut in this order if behind:** static HTML report → decision-trace CLI →
chargeback penalty term → holdout harness → cost matrix.

**Never cut:** C7, C8, NOT_BUILT.md, or pitch/interview prep.

**Explicitly not built:** trained models, live webhook infrastructure as the
spine, React dashboard, real SMS/WhatsApp, multi-gateway abstraction,
portfolio optimisation, contextual bandits, goodwill scores, calibration loops.
If a change drifts toward any of these, stop and flag it.

Build to "defensible in an architecture review," not to production standard.
Sessions are single-objective — do not drift into the next component.

---

## Hard rules

**Never:**
- LLM in the money-decision path
- Train a model — no real labels; a model fit to our own generator learns the numbers we typed
- Propose sub-daily retry timing for mandate debits (PDN + peak-hour constraints)
- Model a programmatic domestic card retry
- Live webhook infrastructure as the spine, React dashboard, real SMS/WhatsApp, multi-gateway
  abstraction
- Add a cardinal assumption without recording it in `ASSUMPTIONS.md` with a source
- Silently swallow a guard block

**Always:**
- Config-driven YAML — the full comparison must re-run with changed parameters in seconds
- Append-only audit events; every decision reconstructable
- Deterministic and seeded; `python -m recovery.reproduce` regenerates every README number
- Small, frequent, legibly-messaged commits

## Do not write these for me

Panel will interrogate these; they must be hand-authored. Offer review, tests, typing —
not implementations.

- The attempt allocator (`allocator/`)
- The four-class taxonomy mapping and the cost matrix
- `ASSUMPTIONS.md`, `PRIOR_ART.md`, `NOT_BUILT.md`, `THREAT_MODEL.md`

**Delegate freely:** schemas, dataclasses, simulator scaffolding, world-parameterisation
plumbing, Hypothesis generators (invariants specified by hand), Razorpay adapter, report
generation, CLI plumbing, fixtures, refactors.

## Stack

Python 3.11+. Pytest + Hypothesis. SQLite. Pydantic. YAML config. Typer. Static HTML report.
Pinned deps. No dependency added without asking.

Test mode: `failure@razorpay` VPA and card `4000 0000 0000 0002` generate real failure
payloads for fixtures. `order.attempts` increments per failed attempt against an order.

## Still open

- NPCI primary circular text — cite secondary sourcing explicitly until found
- Whether `auto_represent_on_failure` bypasses a fresh PDN for technical declines
- One PSP's docs claim a failed *first* presentation auto-revokes the mandate. Severe if true.
  **Verify before relying on it**
- Bank-holiday calendar for T−1 / T−3 shifting — which calendar, varies by bank?
- Is a payment-failure nudge promotional or transactional under DLT? State both implications

## Definition of done

1. It has a test
2. Its decisions are visible in the audit trail
3. `python -m recovery.reproduce` still passes
4. Any new assumption is recorded and classified
5. It can be explained out loud without reading the code
