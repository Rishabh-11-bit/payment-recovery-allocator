# NOT_BUILT

Everything considered and rejected, with the reason.

**Why this file exists.** A scope list that only says what was built invites the
question "why not X?" for every X, and answering it live is worse than answering it
in advance. More usefully: several of these were rejected on **correctness**
grounds, not on time. That distinction is the interesting one, and it is lost if
the whole list reads as "ran out of days."

---

## Rejected on correctness

These would have been wrong to build, not merely expensive. If time were unlimited
they would still be absent.

### Trained classification model

A model needs labels. There are no real ones: the only labelled failure data in this
repo is five captured test-mode payloads, and the class labels on the synthetic batch
were assigned by an emission table I wrote.

So a model trained here learns the parameters I chose. Its accuracy would measure my
consistency with myself, and reporting it as evidence would be the same circularity
recorded in `CHALLENGES.md` 002 — arriving through a different door.

The deterministic lookup table has the opposite property: it is wrong in ways a
reader can *see and dispute*, because every row is a claim with a note attached.

*Source: `CLAUDE.md` "Explicitly not built"; `CHALLENGES.md` 002.*

### Free-text `error_description` parsing — BUILT, and measured

This entry used to say the one place a model belongs was not built. It is now C13,
so what follows is the result rather than the reason.

`llama3` via a local Ollama reads `error_description` and returns **markers** —
observations about what the sentence says, never a class and never a number. An
authored map in `config/classifier.yaml` turns a marker into a `cause_family`,
which shapes what a contact says. It cannot touch the class, the band, or whether
an execution is spent.

**Measured on the three distinct captured descriptions: one changed a
classification.**

| enum key → band | markers | effect |
|---|---|---|
| `international_transaction_not_allowed` → HIGH | `merchant_configuration` | none — enum sufficed |
| `payment_failed` → LOW | `bank_referral` | none — taxonomy already names the family |
| `payment_cancelled` → LOW | `customer_abandoned` | sets `cause_family=cancel_reason_unknown` |

The model was right all three times and mattered once. That is the shape to expect:
it can only bite on a LOW band with no authored family, because everywhere else the
enum key was already sufficient — **which is the argument for a deterministic
classifier, not an apology for one.**

Three strings cannot measure a parser, and the interesting case is missing
entirely: UPI is the primary rail, test mode never exposed it, and no UPI
description has been read by anything.

**What stayed out.** A hosted API. It would put a key and an external service
inside the path `python -m recovery.reproduce` has to keep reproducible, and the
version behind that endpoint is not something this repo can pin. Local inference
plus a committed cache gives the same evidence with neither.

### Contextual bandits

Same circularity, worse. A bandit does not just learn the generator's parameters, it
optimises against them — it would converge on whatever my recovery curves reward and
report the resulting uplift as a result.

*Source: `CLAUDE.md` "Explicitly not built".*

### Calibration loops

Feeding realised outcomes back into the world parameters closes the loop between the
thing being measured and the thing doing the measuring. On synthetic traffic that is
a machine for producing agreement with itself.

The honest version of this idea is C12: measure on real traffic through a holdout,
where the outcomes are not ones we generated.

*Source: `CLAUDE.md` "Explicitly not built".*

### Customer goodwill scoring

Invents a psychological state with no observable ground truth. Nothing in any
available data records how a customer felt about a notification — only whether the
mandate survived, which is already counted.

A goodwill score would let the allocator justify decisions against a quantity nobody
can check, which is the failure mode this project spends most of its discipline
avoiding.

*Source: `CLAUDE.md` "Explicitly not built".*

### Card-change conversion uplift

Almost certainly real: a card-change offer on a dead card should convert better than
a generic recovery link. It is deliberately **not** modelled.

Modelling it would introduce a new invented cardinal whose effect is to favour the
arm that proposes the action — Arm C. There is no source for the magnitude. So the
environment prices a card-change offer and a generic link identically, and Arm C's
TERMINAL advantage is measured only as attempts and notifications saved, which is
definitional.

**This understates Arm C**, and the understatement is the point: an arm that wins
without the parameter that would flatter it is a stronger claim than one that needs it.

*Source: `recovery/sim/environment.py`, `Proposal` docstring.*

### Single-point LTV figures

A mandate is an annuity, so multiplying preserved mandates by an assumed remaining
lifetime produces a large, quotable rupee figure. It would be a cardinal claim
dressed as a result.

Reported as a horizon sensitivity instead: the crossover lifetime at which
preservation outweighs cycle recovery, swept across the hazard range.

*Source: `CLAUDE.md` reporting rules; `recovery/sim/horizon.py`.*

### Mandate-count reporting

Reporting "N mandates preserved" requires a per-notification revocation hazard, and
nobody publishes one. Replaced by a dominance ordering — whether one arm preserves
more than another at *every* hazard in the swept range — which is ordinal and needs
no rate.

*Source: `ASSUMPTIONS.md`; `recovery/sim/run.py` `mandate_survival_dominance`.*

### Per-class revocation multipliers

Built, then deleted. The model originally applied a higher revocation hazard to
LIQUIDITY failures than to technical ones — encoding "being told you are broke is
worse than a technical decline." That is a claim about customer psychology with no
observable ground truth, which is the stated reason goodwill scoring is rejected
above.

Rather than argue it, I flattened the multipliers to 1.0 and re-ran the sweep. The
crossover survived: win rates moved by at most a point, the dominance ordering was
identical, and the breaking point was unchanged. So the parameter was deleted.

The mandate-survival argument now leans on exactly one unsourced cardinal instead of
two.

*Source: `CHALLENGES.md`; `config/worlds.yaml` history.*

---

## Rejected on scope

These are defensible builds. They were not the best use of the remaining hours.

### Live webhook infrastructure as the spine

Days of work — public endpoint, signature verification against a real secret,
retry/backoff handling, deployment — to demonstrate a property the adapter already
demonstrates. The event core is written against the documented delivery semantics and
tested against them: at-least-once, out-of-order, `x-razorpay-event-id` as the dedup
key.

*Source: `CLAUDE.md` "Explicitly not built".*

### React dashboard

Presentation, not architecture. The judging is an architecture review and a technical
interview; neither opens a browser. `python -m recovery.explain <id>` answers the
question a dashboard would be built to answer, and does it in a terminal that is
already open.

*Source: `CLAUDE.md` "Explicitly not built"; cut list.*

### Real SMS / WhatsApp delivery

Requires DLT registration under the TRAI framework, with a header and template
registration process that is not same-week. The decision being demonstrated is
*whether and when to contact*, and a console adapter demonstrates that decision
identically.

The open compliance question underneath — whether a payment-failure nudge is
promotional or transactional — is a real one and is written up in
`DLT_COMPLIANCE.md` rather than resolved.

*Source: `CLAUDE.md` "Explicitly not built" and "Still open".*

### Multi-gateway abstraction

An interface over several PSPs. It would be speculative generality: there is one
gateway in scope, the adapter boundary already exists (`SimulatedExecutor` primary,
`RazorpayExecutor` demonstrated), and a second implementation is what would reveal
whether the abstraction is right.

*Source: `CLAUDE.md` "Explicitly not built".*

### Portfolio optimisation across a fixed intervention budget

Treating the whole batch as one constrained optimisation rather than deciding case by
case. The constraint only binds where interventions are scarce across cases — with a
per-case contact budget and no shared pool across merchants, the batch-optimal
allocation and the per-case decision coincide.

The difference would appear at merchant scale, where a fixed daily contact quota is
shared across thousands of cases. That is not demonstrable here, and building the
machinery for a regime the evaluation cannot exercise would add code a reviewer
cannot verify.

*Source: `CLAUDE.md` "Explicitly not built".*

---

## Rejected as out of boundary

Not ours to build, regardless of time.

### In-session fallback routing

Razorpay's Optimizer already does this. Building it would mean pitching a product
they ship. C10 is deliberately the *out-of-session* case: the link sent after the
customer has gone, where there is no session to fall back within.

*Source: `CLAUDE.md` prior art table; `recovery/rail_actions.py`; `CHALLENGES.md` 001.*

### Programmatic domestic card retry

Not built because it does not exist. Manual charging of a domestic card is not
supported, so card re-attempts are customer-mediated. Modelling one would have made
the evaluation measure actions the platform cannot take.

*Source: `CLAUDE.md`; `CHALLENGES.md` 004.*

### `ATTEMPT_NOW`

Absent from the action space by construction. Every mandate execution needs a
pre-debit notification in advance — 25 hours for UPI, 36 for cards — and must land
outside peak hours, so there is no "retry now" to offer.

*Source: `CLAUDE.md` regulatory constraints.*

### A merchant-facing retry configuration surface

Razorpay's Intelligent Retry Engine is exactly this. This project is what would sit
*behind* an "auto" setting, not another set of knobs in front of it.

*Source: `CLAUDE.md` prior art table.*

### In-session card retry

Razorpay's In-Session Retries, launched July 2026, handles the customer-present card
case: retry from the same payment session, failure reason shown, alternate card
without leaving checkout — and every retry stays on the same Payment ID. Ours is the
customer-absent case on a mandate rail.

Worth noting that their design makes the same distinction as `CHALLENGES.md` 008:
some attempts consume the counter and some do not.

*Source: Razorpay product announcement, July 2026; `PRIOR_ART.md`.*

---

## Deferred, not rejected

Would be built next. Listed so the boundary between "chose not to" and "ran out of
time" stays visible.

| Item | Why it is not here |
|---|---|
| A real UPI Autopay capture | Test mode never exposed UPI. It is the project's primary rail and the least evidenced part of the classifier — the single highest-value fixture still missing. See `ASSUMPTIONS.md`. |
| Static HTML report | First on the cut list. `reproduce` prints the same figures. |
| Decision-trace CLI beyond `explain` | Second on the cut list; `explain` covers the case the panel will ask about. |
| Chargeback penalty term | Third on the cut list, and the reason is narrower than "no source". The *cost* is observable: `amount_deducted` on the dispute entity is 0 while open or won and the full amount when lost, so a realised chargeback prices itself. What has no source is the **rate** — how often a recovery attempt provokes a dispute — and a penalty term needs the rate, not the price. Disputes are also rare on authorised mandate debits, where the customer already granted standing permission. |
| Manual-recovery rate for halted subscriptions | Would make the `WORKING` survival basis non-degenerate. Nobody publishes it. |
| Ingesting `subscription.pending` / `subscription.halted` | The mandate-level view of the same failures, carrying `auth_attempts` and `remaining_count`. `payment.failed` carries the error fields; both would be needed, joined on the chain. Four `mandate_creation_*` taxonomy rows are unreachable without it and are marked as such. |

---

## Revenue leaks identified and not addressed

Found during research, deliberately out of scope. Listed because "we did not look" and
"we looked and chose not to" are different statements, and only the second is a
position.

### Halted subscriptions with chargeable issued invoices

**The most annoying one to leave.** A skipped invoice stays chargeable after halt, and
charging it does not consume a retry — so it is recoverable revenue at **zero cost
against the NPCI cap**. The documented baseline abandons it entirely: it halts and
stops.

Not built because it is a different system. It is a collections workflow over a static
list of chargeable invoices, not an allocation problem — there is no scarce resource to
allocate, which is the entire subject here.

No volume figure is available for how many halted subscriptions carry chargeable
invoices, so no claim is made about its size.

### Mandate registration drop-off

Razorpay's own figure is ~30% of subscribers dropping off before registration completes
— by that measure, the largest leak named in their own material. Large, real, and
**already covered by their Intelligent Retry Engine**, which explicitly addresses
registration drop-off with a pre-filled registration link.

Building it would duplicate a shipped product — the mistake `CHALLENGES.md` 001 exists
to record. It is also unreachable from our event source: `payment.failed` is not
triggered when a payment fails during authorisation.

### Late-authorised payments auto-refunded after 5 days

A payment that late-authorises and is never captured is auto-refunded within 5 days.
That is money that arrived and was given back.

Not built because it is a capture-window problem, not a retry-allocation one, and
because acting on it means capturing payments the merchant may not expect — a
materially different risk posture from scheduling a retry. The safety invariant here
exists to *avoid* creating obligations in that window; harvesting it would mean
deliberately operating inside the window this system is designed to respect.

### Paused subscriptions that never resume

A pause is not a cancellation, and a subscription paused indefinitely is revenue
stopped without anyone deciding to stop it. `pause_initiated_by` distinguishes
merchant-initiated from customer-initiated, and a **customer-initiated pause cannot be
resumed by the merchant** — so only one half of the population is even addressable.

Not built because the addressable half needs a nudge-to-resume flow, which is contact
policy with no execution budget attached, and because OC125 removes pause/cancel
entirely for lending merchants — so the population varies by merchant category in a way
nothing here models.
