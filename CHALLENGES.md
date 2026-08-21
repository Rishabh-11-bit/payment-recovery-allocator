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
| 1 | _(fill in)_ | | |
| 2 | | | |
| 3 | | | |

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

## 003 — _(next entry)_

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

- [ ] Does the Intelligent Retry Engine cover **one-time** payment failures or only recurring
      debits? Scope depends on the answer.
- [ ] 1+3 retry cap — verify against NPCI primary source. If unverifiable, phrase as
      "as documented by Razorpay," never as a regulator citation.
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
