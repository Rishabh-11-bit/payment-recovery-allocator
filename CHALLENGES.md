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

`#domain` `#architecture` `#data` `#evaluation` `#integration` `#safety` `#scope` `#compliance`

---

## Where to start

Four entries carry most of the judgment, if you are reading rather than searching:

- **017** — the allocator's main action was dead in production and every test was green
- **002** — the evaluation was measuring its own assumptions
- **009** — the safety invariant held perfectly while the system stopped working
- **015** — a failure channel that contained something which was not a failure

**005 is deliberately kept despite duplicating 004.** It is the same constraint found a
second time, as a naming problem rather than a platform one, and the fact that one
constraint could hide inside two different mistakes is the point of leaving both.

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

## 007 — A validation rule built from the documentation rejected real production data

**Date:** Phase 1
**Tags:** `#data` `#integration` `#domain`

**Problem**
The classifier key is `(method, source, step, reason)`, and the legal `source` values differ
per method. I built that value space from Razorpay's error-parameters reference and validated
against it: a source outside its method's documented set was treated as anomalous, refused a
rule match, and dropped to the unmapped fallback. It felt like rigour. Every payload I had was
one I had written myself, and they all passed.

Then I captured five real test-mode failures. One was a netbanking decline with
`error_source: bank`. The reference lists netbanking sources as `customer`, `business`,
`internal`, `issuer_bank` — no bare `bank`, which it documents only for emandate. My own
context file had written the rule down as a fact: "no bare `bank` except for Emandate."

So the classifier rejected an entirely ordinary netbanking failure. Not a malformed payload,
not an edge case — the single most common way a netbanking payment fails.

**Diagnosis**
The check was sound; the premise was not. I had treated the documentation as an *enumeration*
of what the API returns, when it is a *lower bound* on it. Reference docs are written to
describe the common cases, they lag the implementation, and nobody updates them when a new
source value starts appearing.

The failure mode generalises: **any strictness check derived from documentation will reject
valid production data wherever the documentation is incomplete.** And the incompleteness is
invisible from inside — the check passes everything you built from the same doc, so it looks
correct right up until it meets real traffic.

What made it worse is where the rejection landed. An unmapped key gets the cost-model fallback
and a LOW confidence band, which is the *correct* handling for a key we genuinely cannot read.
The netbanking generic decline would have gone there anyway on its merits. But it would have
arrived there for the wrong reason — flagged as an anomalous payload rather than as an
uninformative one — and a payload type that resolved cleanly would have been rejected the same
way. The bug was hidden behind a fallback that made its output look reasonable.

**Options**
1. Add `bank` to the netbanking set and move on — rejected. It fixes this instance and leaves
   the mechanism intact, so the next undocumented value fails the same way
2. Drop the value-space check entirely — rejected. The check has real value: `razorpay` is
   genuinely not a source, and a UPI-only source appearing on a card is worth seeing
3. Keep the check, change what it does. Surface the anomaly; never reject on it

**Resolution**
Option 3. The value space is now documented in code as a lower bound rather than an
enumeration. A source outside its method's set is recorded on the classification
(`source_undocumented`), audited as `failure.source_undocumented` with an explicit
`action: surfaced_for_review`, and classification proceeds normally with confidence untouched.
`bank` joined the netbanking set on the evidence of the capture, not on the documentation.

The test that asserted "bare `bank` is emandate-only" now asserts the opposite and carries a
comment saying it previously encoded the documentation rather than reality. `CLAUDE.md` was
corrected the same way.

**Why it mattered**
The rejected payload was not exotic. `payment_failed` from `bank` at `payment_authorization`
is the generic netbanking decline — Razorpay's own `error_description` for it says only "try
another payment method or contact your bank." It will be one of the highest-volume keys in any
real batch. A classifier that discards its most common input is not conservative, it is
broken, and it would have looked fine in every test I had.

The wider lesson is about where strictness belongs: **validation that rejects belongs at a
trust boundary; validation that describes belongs everywhere else.** I had put a
trust-boundary check in a place where the only thing crossing the boundary was my own
incomplete understanding — so the cost of being wrong fell on valid data rather than on the
assumption. Five real payloads found it. I had written dozens of synthetic ones and every
single one agreed with me.

---

## 008 — The attempt cap counts one thing; I had been counting another

**Date:** Phase 1
**Tags:** `#domain` `#compliance` `#architecture`

**Status:** RESOLVED in Phase 3, by documentation rather than by choosing. See Resolution.

**Problem**
All five captured test-mode failures came back against a single `order_id`. My first reading
was that this was an artefact of me retrying the same order in the dashboard while capturing
fixtures.

It is not an artefact. It is what a Payment Link does: every attempt against the link resolves
to the same order, and `order.attempts` increments per attempt. A customer who opens a recovery
link and tries three times produces exactly that shape in production.

Which matters, because a case in my model is keyed on `order_id` and I assign each payment in
the chain a position — so that customer's three link attempts arrive as attempts 2, 3 and 4
against a cap of 4, and the fourth system-initiated retry has nowhere to go.

**Diagnosis**
**The cap of 4 is a mandate-execution cap, not a payment-attempt cap.** They are different
counters over different populations:

| Counter | What increments it | Consumes the NPCI budget |
|---|---|---|
| Mandate executions | System-initiated debits against the mandate, by sequence number | **Yes** — 1 initial + 3 retries, ever |
| Payment attempts | Any attempt against the order, including a customer tapping a recovery link | No |

`order.attempts` counts the second. NPCI caps the first. I had one counter doing both jobs, in
two places: `chain_attempts` in the store assigns a position per payment in the chain, and the
simulator's `attempts_used` starts at 1 and is checked against `attempt_cap`. Both conflate a
customer-initiated attempt with a system-initiated execution.

The error is asymmetric, and in an awkward direction:

- **Over-count** — treat customer link attempts as budget spend. Consequence: surrender
  mandates that still have executions left, forfeiting recoverable money. Safe, and wrong.
- **Under-count** — miss a real execution. Consequence: exceed the NPCI cap. That is a
  compliance breach with UPI API access restrictions and onboarding suspension behind it.

So the conservative direction is to over-count, which quietly makes the allocator worse at
exactly the thing it exists to do — and does it invisibly, because a surrendered case that
still had budget looks identical to a surrendered case that did not.

**Options**
Candidates, none chosen yet:

1. Two counters on the case: `executions_used` (system-initiated, checked against the cap) and
   `attempts_seen` (everything against the order, for reconciliation and diagnostics)
2. Derive execution count from the mandate sequence number rather than from our own tally,
   making it the rail's number rather than ours
3. Tag every payment by initiator at ingest, so the distinction is a property of the record
   rather than something inferred later

**Resolution**
**Deferred first, then resolved — and the deferral is why the resolution is not a guess.**

*Phase 1, deferred.* The fix touches the allocator's budget reasoning (C3) and the guard's
cap check (C4), both hand-authored. Fixing the counter before the policy that reads it
existed would have meant guessing at the interface.

*Phase 3, resolved.* Razorpay's own documentation settles both halves, and neither had to
be inferred:

- **A manual charge attempt does not count toward the remaining retries.** Stated
  directly. So the two counters are real, and the platform draws the line where the
  diagnosis above said it was.
- **`auth_attempts` on the subscription payload is the authoritative execution count** —
  1 on `subscription.pending`, 4 on `subscription.halted`.

The guard now takes `auth_attempts` on the `GuardRequest` and lets it **win outright** over
the `chain_attempts` heuristic, which survives only as the fallback for a case that has
seen no subscription event yet. That ordering is the point: the authoritative number is
used when it exists, and the inferred one is visibly a fallback rather than a peer.

This is also why the asymmetry argument in the Diagnosis did not have to be acted on. The
plan had been to pick the safe direction and over-count. Waiting meant not having to pick
at all — **the system now records which attempts it initiated instead of inferring it**,
which is what the entry said the right answer would look like.

Recorded now, with a note added to `CLAUDE.md` under the regulatory constraints, so the
distinction is visible before C4 starts rather than rediscovered inside it.

The constraint any fix must satisfy: **the system already knows which attempts it initiated.**
The information is available at the moment of the decision — it is simply not modelled. Nothing
here requires inferring initiator from a payload after the fact.

**Why it mattered**
The cap is the scarce resource the entire project is about allocating. Miscounting it is not a
detail at the edge, it is an error in the denominator of every claim the allocator makes.

It also came from the same place as 007: a shape in real data that I explained away. The
shared `order_id` was visible the moment the fixtures landed, and my first instinct was to
attribute it to how I had captured them rather than to what the platform does. Two entries in
a row where the real payloads knew something the design did not, and in both cases the tell was
me finding a reason the data did not count.

---

## 009 — The safety invariant held perfectly while the system quietly stopped working

**Date:** Phase 2
**Tags:** `#safety` `#architecture` `#evaluation`

**Problem**
C7 generates adversarial event orderings and hunts one invariant: never create a payment
obligation outside the original order's attempt chain. One of the generated hazards is a
worker crashing between claiming a job and finishing it. Thousands of orderings including
that crash ran clean.

They ran clean because the crash produces *no* obligation. The job is left in `claimed`,
nothing reclaims it, and the event is dropped. Silently, permanently. The invariant was
satisfied in the most complete way possible: nothing happened at all.

**Diagnosis**
`claim_timeout_seconds` had been in `config/default.yaml` since the first scaffold commit,
described in a comment as "a claimed job older than this is considered abandoned and may be
re-claimed." Nothing read it. `claim_jobs` selected `WHERE state = 'pending'` and no code
path ever moved a row out of `claimed`.

So the config documented a behaviour the code did not have, and the tests agreed with the
code because every test that crashed a worker asserted safety properties — no duplicate
decision, no obligation outside the chain — all of which a dropped event satisfies
trivially.

The real diagnosis is about the shape of the property, not the bug. **A safety invariant
says "nothing bad happens". Doing nothing is the easiest way to satisfy it.** Every
liveness failure — the recovery that never runs, the case that never closes, the queue
that silently stops draining — passes a safety-only test suite perfectly.

For this system that failure mode is not academic. The whole thing exists to recover
payments. An event core that drops a `payment.failed` on the floor is indistinguishable,
from the safety tests' point of view, from one working correctly, and the money simply
never arrives.

**Options**
1. Leave it: the mitigation is "run the worker again" — rejected, nothing re-enqueues the
   job, so running again does not help
2. Remove `claim_timeout_seconds` from config, since nothing used it — rejected, that
   resolves the inconsistency by deleting the correct half
3. Implement reclaim, and add liveness assertions alongside the safety ones

**Resolution**
Option 3. `claim_jobs` now also selects rows in `claimed` whose `claimed_at` is older than
the configured timeout, and fails a job that has exceeded `max_attempts_per_job` rather than
reclaiming it forever — a job that kills every worker it touches should stop visibly, not
loop.

Two tests were added that assert something *happens*: a crashed job is reclaimed and
produces its decision, and a poison job ends in `failed` rather than cycling.

**Why it mattered**
The bug is small. What it exposed is not: the entire C7 suite was built to prove a safety
property, and a safety property cannot fail when the system does nothing. I had been
reading a clean run as "the event core is correct" when it only ever meant "the event core
is not unsafe".

Those are different claims and the second is much weaker. It also inverts the usual
intuition about which failures are dangerous — a duplicate charge is loud, a customer
complains, someone investigates. A dropped recovery is silent, indistinguishable from a
failure that was never recoverable, and shows up only as slightly worse aggregate numbers
that nobody can attribute to anything.

Worth stating to the panel as the reason both kinds of property are in the suite, rather
than presenting the safety search alone and letting them find the gap.

---

## 010 — The tool built to keep me honest was overstating its own sample size

**Date:** Phase 2
**Tags:** `#evaluation` `#safety`

**Problem**
The C7 search takes a budget of sequences, runs them, and returns a report whose whole
purpose is to make one claim quantifiable: *this many adversarial orderings were explored
without finding a violation.* The number is the claim; without it "I could not break it"
means nothing.

The search stops at the first violation — there is no point continuing once the invariant
is broken. And the report was constructed like this:

    return SearchReport(
        sequences_explored=sequences if not violations else sequences,
        ...
    )

Both branches return the budget. A search that stopped at sequence 5 of 5,000 reported
5,000 explored.

**Diagnosis**
The conditional expression is the tell: someone — me — wrote it intending to distinguish
the two cases, and then put the same value in both branches. It reads as though a decision
was made. Nothing flagged it, because on a clean run the budget and the count are equal,
and every run had been clean.

That is the uncomfortable part. The bug was invisible in exactly the situation the tool is
used in, and would only have surfaced the first time the search actually found something —
which is the moment its output matters most and gets read most carefully.

**Options**
1. Report the budget and note that it stops early — rejected, that is the bug with a
   comment on it
2. Do not stop early, so budget and count always agree — rejected, it wastes minutes
   re-confirming a known failure, and the first violation is the one that gets debugged
3. Count what actually ran

**Resolution**
Option 3, plus a test that plants a bug and asserts the reported count is *lower* than the
budget. The count is now incremented per executed scenario, and the early stop carries a
comment saying why the distinction matters.

**Why it mattered**
The project's entire argument is about not overstating what the evidence supports —
ordinal rather than cardinal claims, dominance orderings instead of counts, LTV as a
sensitivity rather than a point estimate. The one piece of tooling built specifically to
quantify a claim honestly was inflating its own denominator.

The narrow lesson: a measurement whose two branches return the same value is not a
measurement. The broader one is that discipline applied to results does not automatically
extend to the instruments producing them, and instruments are held to a lower standard
precisely because they feel like plumbing rather than findings.

Every number this repo reports now comes from something that counts what happened rather
than what was requested. That is a small change and I would rather have found it myself
than have a panel ask what the number meant.

---

## 011 — A finding about identity is only as good as the key you matched on

**Date:** Phase 2
**Tags:** `#data` `#domain`

**Problem**
Four months of NPCI per-bank downtime data. The obvious question is whether outages are
transient and rotating or whether particular banks are persistently bad, because the answer
decides what admission control should look like: a static blocklist for a fixed set of bad
banks, or a responsive governor watching current conditions.

Counting bank names across the four files gave a clean answer. 29 distinct banks, 18
appearing in exactly one month, and exactly one — Central Bank of India — appearing in all
four. Mostly rotating, with a single persistent outlier. A tidy finding.

**Diagnosis**
It was wrong, and it was wrong in the direction that made it tidy.

The published files spell four banks two different ways across months:

    India Post Payments Bank Limited  /  India Post Payments Bank Ltd
    Punjab National Bank              /  Punjab national Bank
    Telangana Grameen Bank            /  Telangana Grameena Bank
    Airtel Payments Bank Limited      /  Airtel Payments Bank Ltd

Two are Limited-versus-Ltd, one is a stray lowercase word, one is a vowel. Normalising for
those gives **25 distinct banks, 12 appearing once, and two banks in all four months** —
Central Bank of India and Punjab National Bank.

Punjab National Bank had been split across two spellings, so it read as three months out of
four and fell into the "rotating" bucket. It is not rotating. It is persistent, and its
downtime went 1.67h, 8.80h, 9.40h, 7.95h — starting low and settling five times higher,
while Central Bank oscillates around a stable level.

So the corrected finding is not "mostly rotating with one persistent outlier". It is **two
persistent banks, one steady and one that got worse during the observation window.**

**Options**
1. Report the raw count and note the spelling variance — rejected, that reports a number
   known to be wrong alongside the reason it is wrong
2. Normalise and report only the normalised count — rejected, it hides that the published
   data has this defect, which a reader checking against the source will hit immediately
3. Report both, name the merged pairs, and say what changes

**Resolution**
Option 3. `recovery/calibration.py` normalises Ltd/Limited, Grameen/Grameena, case and
punctuation; the inventory reports "29 as spelled, 25 after normalising", lists the merged
pairs, and states that counting them separately makes a bank that appeared every month look
intermittent.

The `bounded-2026` calibration profile models the two populations separately: a rotating
population with a wide share range, and a persistent population of two named banks with
their observed ranges.

**Why it mattered**
The design consequence inverts. "One persistent bad bank" is an argument for a static
blocklist — identify the bank, deprioritise it, done. "Two, one of which was fine in April
and five times worse by June" is an argument for the opposite: admission control has to
respond to observed conditions, because the bank that needs handling next month is not
necessarily one that looks bad today. Punjab National Bank in April looked like every other
transient. That is the case a static list misses.

The generalisable point is narrower than "clean your data" and more useful. **A finding
about identity — how many distinct things, which ones recur — is only as good as the key you
matched on.** Aggregate statistics survive a messy key: total downtime hours were correct
throughout, because summing does not care what the rows are called. Anything that counts
*distinct* entities, or tracks one across time, silently inherits every inconsistency in the
identifier. Those are exactly the questions worth asking of operational data, and exactly
the ones a raw string key answers wrongly without complaining.

---

## 012 — A month that was not a month

**Date:** Phase 2
**Tags:** `#data` `#evaluation`

**Problem**
NPCI's monthly UPI statistics file lists volume and value by month. August-2026 shows 15,198
million transactions against July's 23,658 million — a 36% collapse, in a series that had
been flat at 22-24 billion for four straight months.

A 36% month-over-month drop in national payment volume would be extraordinary, and it would
be the most interesting thing in the file.

**Diagnosis**
It is not a drop. August-2026 covers **19 days, not 31**.

The file gives both total volume and average daily volume, and dividing one by the other
recovers the number of days:

    August:  15,198.45 / 799.9184  = 19.0
    July:    23,658.35 / 763.1726  = 31.0
    June:    22,716.08 / 757.2027  = 30.0

Every other row divides to its true month length. August divides to 19, because the file is
published mid-month and the current month is partial.

**Nothing in the file says so.** There is no "partial" flag, no as-of date, no footnote. The
row looks exactly like the complete ones. And the giveaway is only visible because the
publisher happens to include the daily average as well as the total — with the total alone,
the row is indistinguishable from a genuine collapse.

Average daily volume tells the true story: 799.92 in August against 763.17 in July, the
highest in the series. Volume was *rising*.

**Options**
1. Drop August — rejected, it is real data for the days it covers and the daily average is
   the highest in the file
2. Use it as published and note the caveat — rejected, any month-over-month total including
   August is wrong by 40% regardless of what a note says
3. Record the partiality in the sidecar and use daily averages for any comparison that spans
   it

**Resolution**
Option 3. The sidecar states the implied day count for every row, marks August partial, and
says explicitly that a month-over-month comparison including it compares 19 days against 30
or 31.

**Why it mattered**
This one has not caused a wrong number in this project, because nothing yet uses the monthly
series. That is luck rather than diligence, and it is the reason it is worth logging: the
error mode is silent, it appears in the most-cited file in the set, and it produces a result
that is not obviously absurd. "UPI volume fell 36%" is wrong but plausible enough that
somebody would build a paragraph on it.

The habit that catches it generalises: **published aggregates should be checked against
their own internal arithmetic before use.** The file contained everything needed to detect
the problem — total, daily average, and a month name — and the check took one division. Any
series where a total and a rate are both published can be validated against itself, and a
row that fails that check is telling you something the schema does not.

---

## 013 — A label that inverted the meaning of the data

**Date:** Phase 3
**Tags:** `#evaluation` `#architecture`

**Problem**
`reproduce` prints a per-arm breakdown headed **"proposals blocked by the guard"**. It
exists to keep one distinction visible: an arm that *wanted* to act and was refused is
not the same as an arm that *chose* not to. That distinction is the whole reason the
guard is separate from the allocator.

Arm B showed 50 blocked. Arms A and C showed zero.

The obvious reading is that Arm B — the contact-everything arm — is the one constantly
running into the rules. It is a story that fits: B is the least disciplined arm, so of
course it gets refused most.

**Diagnosis**
Every one of the 50 was Arm B *succeeding*.

Arm B proposes two things per case per tick: a recovery link, and the baseline retry.
When the link converts, the case closes — and the retry submitted immediately after
arrives at a case that is no longer open. Of the 50: **45 followed the contact
recovering the case**, and 5 followed the contact's notification triggering a
revocation. Arms A and C emit at most one proposal per tick and cannot collide with
themselves, which is why their count is zero.

The mechanical error is one line. `Environment.submit` starts:

    if not state.is_open:
        metrics.record_rejection("case_not_open")
        return False

That returns **before the guard is ever consulted**. `case_not_open` is not a guard
verdict and never was; it was being recorded through the same channel as one, and the
display heading then asserted something about it that was false.

So the number said "Arm B was blocked 50 times by the compliance layer" when it meant
"Arm B resolved 45 cases so quickly its own follow-up became pointless."

**Options**
1. Stop submitting once a case closes within a tick — rejected, it makes the number go
   away rather than making it correct, and loses the signal that B proposes redundant
   work
2. Rename the reason to `case_already_closed` and leave it in the same counter —
   rejected, the count is still displayed under a heading about the guard
3. Count it separately, report it on its own line, and pin the distinction with tests

**Resolution**
Option 3. `ArmMetrics.moot_proposals` is separate from `proposals_rejected`,
`Environment.submit` calls `record_moot()` before the guard, and `reproduce` prints two
lines rather than one. Two tests hold it: Arm B has moot proposals and no
`case_not_open` rejection, and Arms A and C have zero moot proposals because they are
structurally incapable of self-collision.

**Why it mattered**
**A wrong label is worse than a missing one**, and this is the clearest example of it I
have hit. A missing number prompts "what happened here?" A number under a wrong heading
prompts a confident conclusion, and here the confident conclusion was the exact inverse
of the truth — the arm's best moments, presented as its constraint.

It would have survived a demo, too. "Why is your contact-everything arm the only one
getting blocked?" is a natural question with an obvious-sounding answer, and I would
have given the wrong one fluently.

The generalisable point is about where reporting bugs hide. This was not a calculation
error: the count of 50 was correct, the events were correctly recorded, nothing was
lost. The defect lived entirely in the *English* attached to the number — in a heading
written when the counter had one meaning, and never revisited when the counter acquired
a second. Aggregation is where semantics quietly go missing, because two things that
increment the same integer become indistinguishable the moment they do.

Worth noting how it was found: not by a test, but by reading the output and asking why
one arm differed from the others. No assertion in the suite would ever have caught it,
because every assertion was about the count, and the count was right.

---

## 014 — Fixtures from one source cannot tell you about another

**Date:** Phase 3
**Tags:** `#data` `#integration`

**Problem**
The classifier reads `(method, source, step, reason)` off the payment entity. Those come
from `error_code`, `error_source`, `error_step` and `error_reason`, and the whole system
reads them as **flat fields** on the entity.

Razorpay also documents a nested `error: { source, step, reason, code }` object. If the
webhook used the nested shape and the code expected flat, every key would normalise to
`-/-/-/-`, classify as unmapped, and land in the LOW row. Safe — one recovery link, no
execution — and completely silent. The system would report 100% coverage of a key space
it was misreading in its entirety.

**Diagnosis**
Checked, and the code is right: webhooks carry the flat fields, and the nested object is
the shape of an *API error response* rather than a webhook payload. Confirmed against
Razorpay's published AsyncAPI schema.

**But the evidence in this repo could never have shown that.** All five captured
fixtures came from an API *fetch* of the payment entity — which also returns flat
fields. So the fixtures were consistent with the code for a reason that had nothing to
do with the webhook path being right. Had the two sources differed, every test would
still have passed and the first real webhook would have been misread.

The tests were verifying agreement between the code and one source, and being read as
verifying agreement between the code and reality.

**Options**
1. Accept both shapes — rejected. Silently coercing a shape that should never arrive
   hides the signal that an assumption broke, which is the same silent-degradation
   failure in a new place
2. Leave it, since the code is correct — rejected. Correct-today with no way to notice
   becoming-wrong is not the same as safe
3. Keep flat-only, document why, and detect the nested shape loudly

**Resolution**
Option 3. `has_nested_error_object` is checked at ingest and audits
`webhook.payload_shape_unexpected` with the consequence spelled out. It should never
fire; if it ever does, the alternative was a silent 100% misread. `FIELD_MAP` carries a
comment stating that flat is the webhook shape and that the fixtures could not have
revealed a difference.

**Why it mattered**
No bug was fixed here, which is the interesting part. **A fixture corpus drawn from one
source cannot validate behaviour against a different source, however many fixtures it
holds.** Five real payloads felt like strong evidence for the whole ingest path; they
were strong evidence for exactly one half of it.

The general shape: when a system reads the same logical data from two places — a webhook
and an API, a cache and its origin, a replica and its primary — tests built from one
place prove agreement with that place. The other path is untested and *looks* tested,
which is worse than visibly untested, because nobody goes looking.

The cheap mitigation is not more fixtures. It is a check that fires when the untested
assumption turns out to be wrong.

---

## 015 — Not every failure is a failure

**Date:** Phase 3
**Tags:** `#domain` `#data` `#evaluation`

**Problem**
Every `payment.failed` opened a case. That is what the event is called.

But a UPI mandate registration fires a validation debit to prove the mandate works, and
that payment is **always `status: failed`** with `error_reason: upi_dummy_payment`,
`source: business`, `step: payment_initiation`. It is not a failure. Nothing was owed and
nothing was lost. It is the *success* path of registration, reported through the failure
channel.

The system treated each one as a recoverable failure: opened a case, classified it,
spent a contact on it, and counted it in the batch every reported figure is computed
over.

**Diagnosis**
A false positive on the happy path — the worst kind, because it scales with success.
Every new mandate a merchant registers produces one. A merchant growing quickly generates
more of these than real failures, so the better the business does, the more the recovery
system's numbers are diluted by events that were never recoverable.

The damage compounds across the whole stack rather than staying local:

- **The contact budget is spent** on a customer whose mandate just registered
  successfully, and who is told their payment failed
- **The batch denominator inflates**, so recovery rate and money-recovered-per-case both
  understate
- **`terminal_attempts_wasted` and every other definitional metric shift**, because the
  population they are computed over is contaminated
- **The classifier reports it as unmapped or misclassified**, and the coverage figure
  degrades for a key that should never have been classified at all

None of that surfaces as an error. Every component behaves correctly on an input it
should never have been handed.

**Options**
1. Classify it as its own class — rejected. It is not a failure class; adding one would
   put a non-failure into a taxonomy of failure causes
2. Filter it in the allocator — rejected. By then a case exists, a classification has
   been recorded, and the batch already counts it
3. Filter at ingest, before a case exists, and audit the filter

**Resolution**
Option 3. `config.ingest.filtered_reasons` lists reasons that are validation artefacts;
ingest audits `webhook.filtered` and acknowledges 2xx without storing or enqueuing. It is
config rather than a constant because it is a list that will grow, and each entry is a
claim about the platform that deserves to be visible.

Acknowledged rather than rejected, deliberately: a non-2xx would push the whole webhook
toward the 24h backoff that disables it.

**Why it mattered**
**A channel named for failures does not contain only failures**, and the assumption that
it does is invisible because the name is doing the arguing.

It also lands squarely on the evaluation discipline this project spends most of its
effort on. Every headline figure is per-case; the denominator is the batch; and the batch
was silently including events that were structurally incapable of contributing to the
numerator. The number would have been wrong in the safe direction — understating — which
is exactly the direction nobody checks.

Worth noting where it was found: reading Razorpay's registration flow documentation, not
the failure documentation. The event is described where mandates are *created*, and a
reader who only studies the retry and failure pages never encounters it.

---

## 016 — Counting the fields is not the same as weighing them

**Date:** Phase 3
**Tags:** `#design` `#classifier` `#silent-failure`

**Problem**
Eight authored rows were added to the taxonomy. Three of them did not work, and the
three that did not work reported nothing.

The rows match on `reason` — `upi_autopay_not_supported_on_psp`,
`funds_blocked_by_mandate`, `reqauth_mandate_not_acknowledged`. Each one names the cause
outright. Each one lost to a rule already in the file that named `method` and `step`:

| Row | Should be | Actually classified |
|---|---|---|
| `upi_autopay_not_supported_on_psp` | TERMINAL 0.95 | INFRASTRUCTURE 0.88 |
| `funds_blocked_by_mandate` | LIQUIDITY 0.88, `funds_committed` | LIQUIDITY 0.92, no family |
| `reqauth_mandate_not_acknowledged` | ATTENTION 0.70, `acknowledgement` | ATTENTION 0.90, no family |

The first is the expensive one. TERMINAL read as INFRASTRUCTURE is the highest cell in
the misclassification matrix — cost 10 — because it spends a capped execution that cannot
succeed *and* buys a failure notification that moves the customer toward cancelling. The
row written specifically to prevent that was in the file, valid, loaded, and inert.

**Diagnosis**
Rule precedence was the count of named fields:

```python
self._rules = sorted(config.rules, key=lambda rule: (-rule.specificity, rule.index))
```

`{method: upi, step: payment_initiation}` names two fields.
`{reason: upi_autopay_not_supported_on_psp}` names one. Two beats one, so the rail-plus-
step rule won.

Counting treats all four key fields as equally informative. They are not, and this
project's own context file already says so — "`step` localises better than `source`" is
written down. `reason` names the cause; `method` is the rail and says nothing at all
about what happened. A rule naming only `reason` is making a *more* specific claim than
one naming `method` and `step`, using fewer fields to do it.

The failure mode is what makes it worth an entry. A shadowed rule is not an error. It
loads, it validates, it appears in the file, it counts toward the rule total, and it
reads as coverage in review. The only way to observe it is to classify a key that should
hit it and check what came back — which is exactly what nobody does for a row they just
wrote and can see sitting there.

**Options**
1. Give the new rows more fields until they win — rejected twice over. It invents source
   and step scope that was never authored, and it fixes three rows while leaving the
   ordering that broke them in place for the next one
2. Keep counting and forbid low-count rules — rejected. `reason`-only rules are the most
   defensible kind in the file; banning them to preserve a sorting bug is backwards
3. Order by which field is named, most discriminating first

**Resolution**
Option 3. `FIELDS_BY_INFORMATIVENESS = ("reason", "step", "source", "method")`, and
precedence became a tuple of "is this field named", compared lexicographically. Field
identity dominates field count; among rules naming the same discriminating field, naming
more of the rest still wins.

**The change was verified inert before it was kept.** Every distinct key the simulator
emits across five world seeds, plus every captured payload — 15 keys — was classified
before and after and diffed. Zero changed. So the new ordering fixes the three dead rows
and touches nothing the project reports.

It has one real cost, and it is not hidden. `_reject_ambiguous` used to catch two rules
at equal field count that overlap and disagree. Under precedence, two rules tie only when
they name the same field set — and then overlapping means identical constraints. The
pairs it stopped catching are the ones the informativeness order now resolves
deterministically, so they are decided rather than undetected; but the net is narrower,
and the test that used to assert the old behaviour now asserts the resolution instead.

A second check went in alongside it, for the same class of silence: a rule naming a
`step` outside `step_space` is now rejected at load. A mistyped step matches nothing, and
a rule that matches nothing is indistinguishable from one whose key has not arrived yet.

**Why it mattered**
**A rule that never fires and a rule that fires correctly look identical everywhere
except at the moment of matching.** Every other signal agrees they are fine: the file
parses, the count goes up, the diff looks right, review passes.

The general shape is worth keeping. When a system resolves between candidates by a score,
check what the score actually measures — here it measured *how many constraints a rule
states*, and it was being read as *how specific a rule is*. Those coincide often enough
to never notice, and diverge exactly where one field carries far more information than
another. The eight new rows did not create this bug; they were simply the first rows
written in a style that could expose it.

Worth noting how it was found: not by a failing test, but by printing the classification
of each new row after adding it. The tests written afterwards pin the outcomes, and they
would have caught it — but only because the printout said to write them that way.

---

## 017 — The allocator's main action was dead in production, and the simulator was green

**Date:** Phase 3
**Tags:** `#integration` `#testing` `#audit` `#threat-model`

**Problem**
`SCHEDULE_AT` could never have been admitted on the live path. Not "was buggy" —
**could never have been admitted**, for any case, ever.

It is the allocator's central action. The LIQUIDITY cells exist to spend an execution
later rather than sooner, which is the one piece of timing freedom NPCI leaves and the
entire content of the "better placement of surviving attempts" claim. On the live path
every one of those decisions was refused at admission.

The whole time, the simulator was green, the sweep was green, C7's adversarial search was
green, and every test in the suite was green.

**Diagnosis**
Four components, each correct:

1. **The allocator** picks a compliant slot. `compliant_slot` applies the non-peak
   window and the rail's PDN lead, and returns a `Proposal` carrying `execute_at`.
2. **The `Decider` protocol** returns `tuple[DecisionAction, str]` — an action and a
   reason. The slot is not in the tuple, so it is discarded at the seam.
3. **The worker** therefore has no slot to pass, and constructed its `GuardRequest`
   with a literal `execute_at=None`.
4. **The guard** checks that an execution names when it will run, finds `None`, and
   refuses with `execute_at_not_in_future`.

Every component did its job. The defect lives in the *space between* them, which is why
no unit test could hold it: each side of the seam was individually right, and the
protocol was the thing that was wrong.

The protocol was written before the allocator existed, deliberately, so C1 could be
proved without C3 — and `PendingAllocatorDecider` only ever returned `HOLD`, which
creates no obligation and needs no admission. **A protocol designed against a
placeholder that never schedules anything will not have a field for when to schedule
it.** The seam was shaped by its first implementation and nobody re-derived it when the
real one arrived.

**Why the simulator could not see it.** The simulator constructs proposals and calls
`Guard` directly. Production goes ingest → worker → decider → guard. The two paths share
the guard and the allocator and *nothing in between*, so the simulator exercises the
allocator's output and the guard's rules while never exercising the thing that carries
one to the other. Both paths were tested. The path that only exists in production was
not, because it is made of the components rather than being one.

**This is `THREAT_MODEL.md` item 8 materialising as a real defect** — the simulator and
the live path diverging where they are not forced to agree. It was written down as a
risk before it happened, which is worth something, and did not prevent it, which is
worth knowing. Naming a risk is not the same as having a test that fails when it occurs.

**How it was actually found.** Not by a test. By running
`python -m recovery.explain pay_SYNTHNOFUNDS01` after wiring the allocator in, and
reading:

```
  OUTCOME    no obligation created - guard blocked: execute_at_not_in_future
             (an execution must name when it will run)

  blocked by the guard:
    SCHEDULE_AT -> execute_at_not_in_future: an execution must name when it will run
```

**That is the difference between an audit trail and a log.** A log would have recorded
that nothing happened. The trail recorded *what the system wanted to do, that it was
refused, and the specific rule that refused it* — three facts, of which only the first
was visible anywhere else. "No decision" and "a decision the guard refused" are
different outcomes, and the trace was built to keep them apart precisely because
conflating them hides working intent behind apparent inaction.

The guard's own design contributed: **every block carries a reason, is audited, and is
attributable.** A guard that returned a bare boolean would have produced the same
silence a log would.

**Options**
1. Have the worker compute the slot itself — rejected outright. It would put scheduling
   policy in the event core, duplicating `compliant_slot` in a second place where it can
   drift, and the whole point of the allocator is that it owns that decision
2. Widen `decide` to return a 3-tuple — rejected. It breaks both existing
   implementations and the test fake, to serve deciders that never schedule anything
3. A `DecisionProposal` return type replacing the tuple — the cleanest design, and
   more churn than the defect warrants at this stage
4. An optional `execution_slot` hook the worker consults only when the action creates an
   execution

**Resolution**
Option 4. `SLOT_HOOK = "execution_slot"`, looked up by name, called only when
`kind is ProposalKind.EXECUTION`. `ArmCDecider` implements it by re-planning and
returning the `ATTEMPT` proposal's `execute_at`. `PendingAllocatorDecider` does not
implement it and should not — a decider with no executions has no slot, and forcing it
to return `None` would be ceremony.

The worker now also passes `rail`, without which the rail-conditional PDN lead cannot be
checked at all — a second thing the seam was silently dropping.

Two tests pin it: one asserts a LIQUIDITY case reaches a *recorded decision* rather than
a block, and one asserts the placeholder has no slot hook so the optionality stays
deliberate.

**Why it mattered**
**Green tests on both sides of a seam say nothing about the seam.** The simulator tested
allocator→guard by calling the guard directly. Production runs
allocator→decider→worker→guard. Testing the endpoints while skipping the wiring is a
structural blind spot, not an oversight — and it is invisible by construction, because
every component passes.

The general shape: when a system has a "real" path and a "test" path that share
components but not composition, the difference between them is untested by definition,
and it is exactly where integration defects live. The fix is not more unit tests. It is
one test that runs the composition end to end — which is what
`tests/test_explain_wiring.py` now is.

Second, and more uncomfortable: **the failure was silent in the direction that looks
safe.** A refused execution produces no obligation, no error, and no alarm. The system
appeared conservative. Conservatism is what this allocator is *for*, so its central
action failing closed looked exactly like it working. A defect whose symptom is
indistinguishable from the intended behaviour will not be found by watching outcomes;
it is found by asking a specific case what it did and why.

Third: this is an argument for building the decision-trace CLI *early* rather than
treating it as a presentation layer. It is second on the cut list in `CLAUDE.md` —
one bad week from not existing. It found a defect that four test suites and a
300-world sweep did not.

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

---

## 018 — The test failed for being quick

**Date:** Phase 3
**Tags:** `#testing` `#flake` `#time`

**Problem**
One run of the suite reported `1 failed, 442 passed`. The next five were clean. Nothing
had changed between them.

An unidentified intermittent failure is worse than a known one: it cannot be reasoned
about, it erodes trust in every green run after it, and on a project whose entire claim
is "the evidence is reproducible" it is the exact defect that most undermines the pitch.

**Diagnosis — including the part that wasted the most time**
The first hunt was **the wrong experiment, run correctly**: 20 full-suite runs with a
fixed Hypothesis seed. All 20 passed, and they could not have done otherwise. Fixing the
seed makes C7's property search explore the *same* orderings every run — it converts the
only randomised component in the suite into a deterministic one. A fixed seed is the
right tool for *reproducing* a known flaky example and the wrong one for *finding* an
unknown one, and 55 minutes went into establishing that.

Twelve more runs with varying seeds: also clean. 32 runs, zero failures.

What finally reproduced it was narrowing to the file rather than the seed. The full
suite takes ~3 minutes; `tests/test_c7_invariants.py` takes 78 seconds, so the same
wall-clock budget buys three times the attempts. It failed on the third run — and it was
not the Hypothesis search at all:

```
FAILED tests/test_c7_invariants.py::test_crashed_job_is_reclaimed_not_dropped
    assert store.claimed_job_count() == 0
E   assert 1 == 0
```

The test simulates a crashed worker — claim a job, never finish it — then asserts the
next `process_pending` reclaims it. It set `claim_timeout_seconds` to **0.001** and
relied on real time passing. The store's reclaim condition is:

```sql
j.state = 'claimed' AND j.claimed_at < ?   -- cutoff = now - claim_timeout
```

So a 1-millisecond timeout asks: *did the two statements between the claim and the
reclaim take longer than a millisecond?* On a fast machine, or when both timestamps land
in the same clock tick, they do not. The job is not reclaimed and the assertion fails.

**The test failed for being quick.** And the failure message — `assert 1 == 0` — says
nothing whatsoever about timing, which is why it read as mysterious rather than as a
race.

The product logic was correct throughout. Reclaim works. The defect was entirely in how
the test asked the question.

**Options**
1. Widen the timeout to 50ms and hope — rejected. It changes the flake's frequency, not
   its existence, and a test that is *usually* fast enough is still a test that fails on
   a loaded CI box
2. Sleep between the claim and the reclaim — rejected for the same reason, plus it makes
   every run slower to fix a problem that is not about duration
3. Remove the dependency on elapsed real time entirely

**Resolution**
Option 3. The claim is **backdated an hour** with a direct update, and the timeout set to
a realistic 30 seconds. The question the test asks becomes unambiguous at any machine
speed: a claim made an hour ago, under a 30-second timeout, must be reclaimed.

**Verified not to have been weakened.** A test made deterministic by making it vacuous is
worse than a flaky one, so the reclaim branch in `store.claim_jobs` was disabled and the
test re-run: it fails, as it must. Determinism was bought by removing the race, not by
removing the assertion.

One unrelated fix went in alongside, and it is worth separating clearly because it is
**not** the cause: `run()` in the same file gave every Hypothesis example the same SQLite
filename, deleting and recreating it each time, while `execute` opens a store it cannot
close if it raises. On Windows, unlinking a file whose handle is still held raises
`PermissionError`. That was the leading hypothesis before the real cause was found, it
was a plausible mechanism, and it was wrong. Each example now gets its own filename —
correct regardless, and recorded as a speculative fix rather than a diagnosis.

**Why it mattered**
**A test that depends on real elapsed time is a test with a hidden performance
assumption.** This one asserted, without saying so, that two Python statements take more
than a millisecond. That is not a claim about the system under test, it is a claim about
the machine, and it was false often enough to fail roughly one run in four in isolation —
diluted to perhaps one in six across the full suite, which is exactly the frequency that
gets a failure dismissed as noise.

Second, on method: **the instinct to reach for determinism by pinning a seed was
actively counterproductive.** Pinning the seed suppressed the only source of variation
the suite had, and 20 green runs then read as evidence of stability when they were
evidence of nothing. The lesson is not "don't pin seeds" — it is that reproducing an
intermittent failure and confirming a fix need opposite settings, and using the second
tool for the first job produces a confident negative result that is worthless.

Third, on economics: the successful hunt narrowed the *unit under test* rather than
adding runs. Three times the attempts per minute is the entire reason it was found at
all, and the earlier hour of full-suite runs bought less information than fifteen minutes
of targeted ones. When hunting something rare, shrink the trial before increasing the
count.

---

## 019 — A number that confirmed the ambiguity claim actually undermined it

**Date:** Phase 3
**Tags:** `#evaluation` `#data` `#domain`

**Problem**
Built a check the taxonomy had never had: does confidence actually predict correctness?
Pool the synthetic batches, compare each classification to ground truth, split by band.

Two results came back that should not have been reassuring, and my first instinct was
that they were:

- **The six deliberately-low-confidence rows scored 88.8% on their own raw guess** —
  higher than the HIGH band's 84.5%. Read carelessly, that says the rows flagged as
  structurally ambiguous were not actually ambiguous.
- **MODERATE was 0/0.** Read carelessly, that says the check is broken.

**Diagnosis**
Neither reading survives looking at which key is doing the work.

**The 88.8% is one key.** `upi/beneficiary_bank/payment_debit_response/mandate_revoked`
is 56% of the entire LOW-with-a-guess sample (593 of 1,059). Its note names a real
ambiguity: the code can mean a genuine customer revocation, or the beneficiary bank
surfacing its own mandate-state error under the same reason. Checked the simulator's
`EMISSIONS` table for the second cause — **there is no channel for it.** The key is
emitted by exactly one true class, TERMINAL, full stop. So a high score here does not
confirm the ambiguity was overstated. It confirms the simulator cannot produce the case
the row exists to guard against — the alternate cause was never in the data the score
was computed over. A synthetic ground truth cannot referee a domain claim about a cause
it does not generate, which is the same shape as the caveat `coverage.py` already
carries about the unmapped-keys list, arriving through a new check rather than a new
insight.

**MODERATE 0/0 is two different facts wearing one number.** Two of the four
MODERATE-band rules (`mandate_creation_expired`, `mandate_creation_timeout`) are
genuinely unreachable — no ingest path produces them, already flagged
`unreachable: true`. The other two (`card_enrollment_check`,
`reqauth_mandate_not_acknowledged`) are **real, reachable rules in production** that the
synthetic generator's `EMISSIONS` table — a fixed, simplified subset of the documented
reason codes — simply never happens to emit. Pointing a reader at the unreachable-rows
section would have been correct for two of the four and silently wrong for the other
two, which is worse than no explanation: it looks authoritative and misleads on exactly
the two rules a reader would most want to trust it about.

**Options**
1. Report the raw numbers with no comment — rejected. An 88.8% next to a bare "LOW,
   informational" label reads as the ambiguity being disproven, and a panel member
   skimming the output would draw exactly that conclusion in one glance
2. Drop the LOW-band figure from the report entirely — rejected. The number is real and
   the mechanism producing it is worth showing; removing it hides a genuine limitation
   of synthetic evaluation rather than stating it
3. Print the number with the specific mechanism that produces it, keyed to the actual
   emission table rather than a general disclaimer

**Resolution**
Option 3. `recovery/coverage.py` now prints the dominant key, its share of the sample,
and the concrete fact that the simulator has no emission channel for the cause the
ambiguity is about — traced with a script before writing a word of the explanation, not
asserted from memory. The MODERATE explanation is split into its two real categories
rather than pointing at one section that is only half right.
`tests/test_horizon_and_coverage.py` pins the emission-channel fact directly against
`EMISSIONS`, not through the printed report, so a second channel being added later
breaks the test that explains the number rather than leaving a stale explanation next to
a number that has moved.

**Why it mattered**
**A synthetic-data metric that happens to agree with an authored judgment is not
evidence for the judgment — it is corroboration bounded by everything the generator
does not model,** and a number that inverts what it looks like it says is far more
dangerous than a number that is simply wrong, because wrong numbers get checked and
inverted ones get believed. The instinct on seeing 88.8% next to a "deliberately low
confidence" label was to read it as vindication. It was the opposite: an absence in the
simulator wearing the shape of a good outcome.

The general form: whenever a check's answer would flatter something already believed,
that is exactly the answer that needs the source traced before it gets written down —
not because it is likely wrong, but because an answer that confirms what you already
think is the one you are least likely to interrogate.

---

## 020 — _(next entry)_

**Date:**
**Tags:**

**Problem**

**Diagnosis**

**Options**

**Resolution**

**Why it mattered**
