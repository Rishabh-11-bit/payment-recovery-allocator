# Build Challenges & Technical Obstacles

Running log. Written as encountered, not reconstructed afterwards.

**Why this file exists:** the Razorpay submission form asks "What issues did you face while
building, and how did you solve them?" That field is a pre-screen for the architecture round.
Answers written from memory on the last day are vague; answers written from a log are specific.

---

## How to write an entry

Every entry has five parts. If an entry is missing the **Why it mattered** section, it is not
finished — that section is the only part a panel actually cares about.

- **Problem** — what actually broke or blocked, concretely
- **Diagnosis** — what the cause turned out to be, and how it was found
- **Options** — what was considered, including what was rejected
- **Resolution** — what was done
- **Why it mattered** — what would have gone wrong if this had been missed

Bad: "Faced API rate limits, added retry logic."
Good: an entry where the diagnosis is non-obvious and the rejected options are named.

**Log it the day it happens.** Dead ends and reversals are the most valuable entries — a build
with no reversals reads as either trivial or dishonest.

---

## Tagging

Tag each entry so they can be sorted when drafting the form answer.

`#domain` `#architecture` `#data` `#evaluation` `#integration` `#safety` `#scope` `#compliance`

---

## Shortlist for the form

Maintain this as you go. Target 3–4 entries, weighted toward `#domain`, `#evaluation`,
and `#scope` — those show judgment. Pure `#integration` entries show persistence, which
is less differentiating.

| # | Entry | Tag | Why it earns a slot |
|---|-------|-----|---------------------|
| 1 | 001 — nearly built a duplicate | `#domain` `#scope` | Audited the incumbent's shipped stack before building, and let the result reframe the project rather than proceeding anyway |
| 2 | 002 — evaluation measured my own assumptions | `#evaluation` `#data` | Identified that a synthetic-data uplift figure proves nothing, and split one claim into three with proof appropriate to each |
| 3 | 003 — strongest result needs no invented probabilities | `#domain` `#evaluation` | Found a claim that rests on a definition rather than a parameter, by reading the retry model line by line |
| 4 | 004 — half the action space does not exist | `#domain` `#architecture` | Discovered the platform cannot execute the actions the allocator was designed to select, in Phase 0 rather than at integration |

**Not shortlisted:** 005 is the same constraint as 004, found a second time as a naming
problem. 004 is the discovery and the stronger entry; presenting both would spend two of four
slots on one insight.

---

# Entries

---

## 001 — The problem was largely already solved, and I nearly built a duplicate

**Date:** Phase 0
**Tags:** `#domain` `#scope`

**Problem**
The obvious build for this track is a cause-aware retry engine: classify the failure, pick a
better retry time, send a recovery link. I had a full architecture for it before checking
whether Razorpay already shipped it.

**Diagnosis**
They largely have. Optimizer / Smart Router already does ML-driven in-session routing with
fallback on failure. Subscriptions already auto-retry on a documented T+1 / T+2 / T+3 schedule
before halting. And an Intelligent Retry Engine was introduced in beta as part of the
Intelligent Revenue-Protect stack, including WhatsApp-delivered branded recovery links.

Building "smarter retries" would have meant pitching Razorpay a product they already have.

**Options**
1. Build it anyway and hope the panel doesn't connect it — rejected, they built it
2. Switch tracks — rejected, the underlying problem is still real
3. Find what the existing stack does *not* cover and build only that

**Resolution**
Option 3. Two gaps survived the audit:

- Retry volume is capped (1 original + 3 retries for UPI Autopay), so the open problem is not
  *retry better* but **how to allocate a fixed, externally-imposed attempt budget**
- Everything shipped optimises for a single merchant. Nobody is looking at the aggregation
  point, even though the stated rationale for the retry cap is network-level load

Wrote `PRIOR_ART.md` as the first commit in the repo, before any code.

**Why it mattered**
This reframed the project from "retry scheduling" to "budget allocation under a regulatory
constraint." It also means the baseline arm in my evaluation is Razorpay's own documented
retry schedule rather than a strawman I invented.

---

## 002 — My evaluation was measuring my own assumptions

**Date:** Phase 0
**Tags:** `#evaluation` `#data`

**Problem**
The standard approach to this problem is: build a synthetic failure generator, run your policy
against it, report money recovered. I had this planned and it looked rigorous.

It isn't. I would be writing the generative model that decides how failures recover, then
writing a policy that exploits it, then reporting the result as evidence. The uplift number
measures nothing except my ability to invert a function I wrote myself.

**Diagnosis**
The failure is that a single number is being asked to carry three different claims —
correctness, robustness, and magnitude — and synthetic data can only support the first two.

**Options**
1. Report the number with a disclaimer — rejected, a disclaimer doesn't make it mean anything
2. Get real data — not available
3. Split the claim into three, each with proof appropriate to it

**Resolution**
Option 3.

- **Correctness / safety** → property-based tests over randomized adversarial event orderings.
  Synthetic data is legitimately conclusive here, because the assertions are invariants, not
  magnitudes.
- **Robustness** → don't build one world, parameterize it. Sample recovery dynamics from
  ranges, sweep across many worlds, and report *where the policy loses* rather than a single
  favourable figure.
- **Magnitude** → do not claim it. Ship the holdout harness that would measure it on real
  traffic instead.

Also constrained the policy to depend only on **ordinal** assumptions ("liquidity failures
recover better later than sooner") rather than **cardinal** ones ("41% on day 30"), and
calibrated the failure mix to published NPCI/RBI data rather than invented distributions.
Every parameter is marked ordinal or cardinal in `ASSUMPTIONS.md`.

**Why it mattered**
"You made up the data" is the first question this project should get. It now has an answer.

---

## 003 — The strongest result turned out to need no invented probabilities

**Date:** Phase 0
**Tags:** `#domain` `#evaluation`

**Problem**
After deciding the evaluation had to be split into three claims (entry 002), the uplift claim
still rested on assumed recovery dynamics. Even ordinal assumptions — "liquidity failures
recover better later than sooner" — are assumptions, and a panel can dispute the ordering.
I wanted at least one component of the result that could not be argued with.

**Diagnosis**
Found it in Razorpay's own subscription retry documentation.

The doc lists four failure reasons: expired card, bank-blocked card, insufficient balance,
and customer-cancelled mandate. It then specifies the retry model — T+1, T+2, T+3, then
`halted` — and the retry model **does not reference the failure reason anywhere**.

So an expired card is retried three times. A customer-cancelled mandate is retried three
times. Those attempts have a recovery probability of zero — not low, zero, structurally, by
definition of the failure. Three consumed attempts from a capped budget, three unnecessary
requests into the banking network, and three failure emails to a customer who already knows
their card is dead.

**Options**
1. Keep the recovered-rupees figure as the headline — rejected, it rests on invented dynamics
2. Report only safety properties and abandon the uplift claim — rejected, too weak
3. Restructure the claim into tiers by how much each rests on invented numbers

**Resolution**
Option 3. The headline result is now **attempts and customer contacts saved on structurally
unrecoverable failures**, which rests on a definitional fact rather than a simulator
parameter: P(same-instrument retry succeeds | instrument expired or mandate cancelled) = 0.

Better placement of the surviving attempts is reported as a secondary claim resting on ordinal
assumptions only. Rupees recovered is not claimed at all — the holdout harness ships instead.

Added `terminal_attempts_wasted` and `terminal_contacts_sent` as first-class metrics.

**Why it mattered**
The primary claim is now one nobody can dispute without disputing a definition. It exists only
because the documented baseline is cause-blind, which I would not have known without reading
the retry model line by line rather than trusting a summary.

---

## 004 — Half the action space I had designed does not exist

**Date:** Phase 0
**Tags:** `#domain` `#architecture` `#integration`

**Problem**
The allocator was designed with `SWITCH_RAIL` as a freely available action, and with
programmatic re-attempt as the default recovery mechanism. Both assumptions were wrong.

**Diagnosis**
Two constraints from the subscriptions documentation:

**Rail migration is a directed graph, not a free choice.**

| From | → Card | → UPI | → Emandate |
|---|---|---|---|
| Card | Yes | Yes | Yes |
| UPI | Yes | No | No |
| Emandate | Yes | No | No |

UPI and Emandate can migrate only to Card. Card is the hub.

**Manual charging of a domestic card is not supported.** For domestic cards there is no
programmatic re-attempt path — recovery is necessarily customer-mediated, via the hosted page
or a card change.

The retry model is also rail-specific rather than uniform. Card and UPI follow T+1 / T+2 / T+3.
Emandate is asynchronous: a retry is attempted only once confirmation or rejection of the
previous payment arrives, which can exceed 24 hours, and charge-day scheduling shifts around
bank holidays (T → T−1, or T → T−3 if both T and T−1 are holidays).

**Options**
1. Model rail switching as unconstrained and note the limitation — rejected, it would make the
   evaluation measure actions that cannot be taken
2. Encode the migration graph and the customer-mediated constraint into the action space

**Resolution**
Option 2. `SWITCH_RAIL` now takes a direction and validates against the migration graph.
Card re-attempts are modelled as customer-mediated rather than programmatic. The simulator
gained a rail dimension with three distinct baseline behaviours, including the asynchronous
Emandate model and bank-holiday shifting.

This also reframed rail exclusion. It is not "exclude the rail that failed" — it is "route
within a directed graph, excluding degraded targets." For a UPI failure the graph offers
exactly one target, so the real decision is whether to migrate at all or hold the attempt,
given that Card is frequently a worse conversion path in India than the UPI that just failed.

**Open question, not an assertion:** Razorpay does not document *why* UPI cannot migrate to
UPI. My inference is that the target rail must be re-authorized, and only cards can be
re-authorized synchronously in-session — a new UPI mandate needs fresh AFA in the customer's
app, and e-NACH registration takes days, so switching would cancel working debit authority in
exchange for authority that may never arrive. This is recorded as a question in the repo, not
as a claim.

**Why it mattered**
An allocator that selects actions the platform cannot execute is not a policy, it is a
simulation of one. Discovering this in Phase 0 rather than during integration saved the
evaluation from measuring impossible moves.

---

## 005 — One name was hiding two different actions

**Date:** Phase 0
**Tags:** `#architecture` `#domain`

**Problem**
The action space conflated two operations under a single name, `SWITCH_RAIL`: mandate-level
rail migration, and link-level checkout shaping. They read as the same action — "change which
rail the customer pays on" — and nothing in the type distinguished them.

**Diagnosis**
They differ in **who executes**.

Link-level shaping the system performs directly: a recovery Payment Link's `options.checkout`
reorders methods or removes a specific degraded instrument, and that takes effect the moment
the link is created.

Mandate-level migration the system cannot perform at all. Manual charging of a domestic card
is not supported, so moving a subscription onto Card is necessarily customer-mediated — via
the hosted page or a card change. The system can only *offer* it.

One is an instruction. The other is an invitation that may never be accepted.

**Options**
1. Keep the single name and document the difference in comments — rejected. The distinction is
   executable-versus-not; a comment does not stop the allocator emitting an action the platform
   cannot carry out, and it does not survive into the audit trail
2. Split into two named actions so the type carries the distinction

**Resolution**
Option 2. `SWITCH_RAIL` became `OFFER_RAIL_MIGRATION` — the verb states that the system offers
and the customer acts — and it validates against the directed migration graph from entry 004.
Link-level shaping stays as `REORDER_RAILS` and `EXCLUDE_INSTRUMENT`.

Final action list: `SCHEDULE_AT(t)`, `RECOVERY_LINK`, `OFFER_RAIL_MIGRATION`, `REORDER_RAILS`,
`EXCLUDE_INSTRUMENT`, `HOLD`, `SURRENDER`. Still no `ATTEMPT_NOW`.

Surfaced by a documentation-consistency audit run before any code was written: the Allocate
section listed one set of actions and the rail-migration section referenced another, and the
two could not both be right.

**Why it mattered**
Entry 004 established that the allocator must not select actions the platform cannot execute.
This is the same failure returning as a naming problem rather than a modelling one — the
constraint had been discovered but not encoded where it would be enforced. A name that hides
the difference between "the system does this" and "the customer might do this" will eventually
be measured as though the two were equivalent, which would inflate the arm-C result with moves
that never happened. Caught in prose, before any code depended on it.

**Form note:** not shortlisted. Same constraint as 004, which is the stronger entry.

---

## 006 — The idempotency key was not stable under the replay it existed to survive

**Date:** Phase 1
**Tags:** `#architecture` `#safety`

**Problem**
C1's whole point is exactly-once: replay one webhook delivery ten times, get one case
and one decision. Exactly-once for decisions rests on a uniqueness constraint over
`recovery:{payment_id}:{policy_version}:{attempt_n}`. I derived `attempt_n` the obvious
way — count the decisions already recorded for the case, add one.

**Diagnosis**
That derivation makes the key a function of *when you ask*, not of *what you are deciding
about*.

Walk a replay through it. First pass: no decisions exist, `attempt_n` = 1, key ends `:1`,
row inserted. Second pass on the same payment: one decision now exists, so `attempt_n` = 2,
key ends `:2` — a key that has never been seen, so the constraint does not fire and a
second decision is written for a single failure. The uniqueness constraint was still
there, still correct, and completely bypassed, because the thing being made unique was
being recomputed differently each time.

It did not show up immediately: C1's ingest dedup catches replays at `x-razorpay-event-id`
before a job is ever enqueued, so the worker never re-ran for the same event. The bug was
real but masked by the layer in front of it. It surfaced when I wrote a test that ran the
worker twice over the same event on purpose, rather than trusting that it could not happen.

**Options**
1. Have the worker check "does a decision already exist for this case" before deciding —
   rejected. That is read-then-write, which races, and it moves the guarantee out of the
   database and into application code that has to remember to ask
2. Derive `attempt_n` from `order.attempts` on the entity — rejected for now: it is a
   field on the order, refreshed at a different time from the payment, and I did not want
   the key's stability to depend on the freshness of a second remote object
3. Assign `attempt_n` once, on first sight of the payment, and store it

**Resolution**
Option 3. A `chain_attempts` table maps `(chain_key, payment_id) -> attempt_n`, assigned
on first sight under a primary key and never updated. `assign_attempt_n` is idempotent:
the tenth call for a payment returns what the first call returned. The idempotency key is
now a stable function of the payment, so replaying anything — an event, a job, a whole
worker pass — recomputes the same key and hits the same constraint.

**Why it mattered**
The failure mode is the one that matters most in this system: a duplicate decision means a
duplicate recovery action, which means an attempt spent twice against a cap of four, and a
second failure notification to a customer the mandate-survival argument says we should be
contacting *less*. It would also have been near-invisible in the metrics — two decisions
on one failure looks like ordinary volume, not like a bug.

The lesson generalises past this bug. A uniqueness constraint only enforces uniqueness of
the thing you actually put in it. Deriving part of that thing from mutable state means the
constraint is guarding a moving target, and it will keep passing while it stops protecting
anything. Worth checking every remaining key in the system for the same shape.

---

## 007 — _(next entry)_

**Date:**
**Tags:**

**Problem**

**Diagnosis**

**Options**

**Resolution**

**Why it mattered**

---

# Watch list

Things likely to become entries. Delete once resolved or ruled out.

- [ ] **GATE** — Does the Intelligent Retry Engine cover **one-time** payment failures or only
      recurring debits? Does WhatsApp recovery extend past subscriptions? Scope depends on it.
- [ ] 1+3 retry cap — verify against NPCI primary source. If unverifiable, phrase as
      "as documented by Razorpay," never as a regulator citation.
- [ ] Why can UPI not migrate to UPI? Inference recorded in 004; no documentation found.
      Good question to put to the panel rather than an answer to assert.
- [ ] Emandate async retry timing — "more than 24 hours" is unbounded in the docs. Needs a
      modelled distribution, and that distribution is a cardinal assumption requiring a source.
- [ ] Bank-holiday calendar for T−1 / T−3 shifting — which calendar, and does it vary by bank?
- [ ] Payments API has no retry endpoint. Recovery is a *new* payment path, not a re-attempt
      of the old one. Confirm how far this constrains the executor.
- [ ] `payment.failed` is provisional — a payment can later become authorized. Late-success
      reconciliation belongs in the core loop, not in hardening.
- [ ] Is a payment-failure nudge promotional or transactional under the DLT framework?
      Different registration, consent and timing rules follow. State as an open question with
      both implications rather than guessing.
- [ ] Coordination boundary if a merchant runs Optimizer and this layer simultaneously —
      both could act on the same failure.
- [ ] Classifier confidence thresholds: misclassification costs are asymmetric, so the
      operating point should follow the cost matrix, not accuracy.
