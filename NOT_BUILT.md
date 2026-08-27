# NOT_BUILT

Everything considered and rejected, with the reason.

**Why this file exists.** A scope list that only says what was built invites the
question "why not X?" for every X, and answering it live is worse than answering it
in advance. More usefully: several of these were rejected on **correctness**
grounds, not on time. That distinction is the interesting one, and it is lost if
the whole list reads as "ran out of days."

> **DRAFT — inference markers.** Anything marked **[INFERRED]** is my reconstruction
> from the repo, `CLAUDE.md` or the build log rather than something you stated. Check
> those before this goes anywhere. Unmarked entries are traceable to a specific line
> in `CLAUDE.md`, a `CHALLENGES.md` entry, or code.

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

### Contextual bandits

Same circularity, worse. A bandit does not just learn the generator's parameters, it
optimises against them — it would converge on whatever my recovery curves reward and
report the resulting uplift as a result.

**[INFERRED]** — the specific "worse than a model because it optimises rather than
fits" framing is mine, not something stated in the repo. The rejection itself is on
the `CLAUDE.md` list.

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

---

## Rejected on scope

These are defensible builds. They were not the best use of the remaining hours.

### Live webhook infrastructure as the spine

Roughly a week of work — public endpoint, signature verification against a real
secret, retry/backoff handling, deployment — to demonstrate a property the adapter
already demonstrates. The event core is written against the documented delivery
semantics and tested against them: at-least-once, out-of-order, `x-razorpay-event-id`
as the dedup key.

**[INFERRED]** — "roughly a week" is my estimate, not a figure from the repo.

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

**[INFERRED]** — the "speculative generality" reasoning is mine; the item itself is
on the `CLAUDE.md` list without a stated reason.

### Portfolio optimisation across a fixed intervention budget

Treating the whole batch as one constrained optimisation rather than deciding case
by case. The constraint only binds at a scale this project cannot demonstrate — with
500 synthetic cases and no shared contact budget across merchants, the optimal
allocation and the per-case decision coincide.

**[INFERRED]** — "only bites at scale I can't demonstrate" is your framing from the
brief; I have not verified the claim that the two coincide at this scale, and it
would be worth a sentence saying at roughly what batch size or budget tightness the
difference would appear.

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
pre-debit notification ≥24h ahead and must land outside peak hours, so there is no
"retry now" to offer.

*Source: `CLAUDE.md` regulatory constraints.*

### A merchant-facing retry configuration surface

Razorpay's Intelligent Retry Engine is exactly this. This project is what would sit
*behind* an "auto" setting, not another set of knobs in front of it.

**[INFERRED]** — stated as a boundary in the prior-art table; naming it as an
explicit rejection is my framing.

---

## Deferred, not rejected

Would be built next. Listed so the boundary between "chose not to" and "ran out of
time" stays visible.

| Item | Why it is not here |
|---|---|
| A real UPI Autopay capture | Test mode never exposed UPI. It is the project's primary rail and the least evidenced part of the classifier — the single highest-value fixture still missing. See `ASSUMPTIONS.md`. |
| Static HTML report | First on the cut list. `reproduce` prints the same figures. |
| Decision-trace CLI beyond `explain` | Second on the cut list; `explain` covers the case the panel will ask about. |
| Chargeback penalty term | Third on the cut list. No source for the magnitude, and it would be another invented cardinal. |
| Resolving the execution-vs-attempt counter | Recorded in `CHALLENGES.md` 008; both readings are implemented and selectable, and the choice is deferred rather than guessed. |
| Manual-recovery rate for halted subscriptions | Would make the `WORKING` survival basis non-degenerate. Nobody publishes it. |


---

## Revenue leaks identified and not addressed

Found during research, deliberately out of scope. Listed because "we did not look" and
"we looked and chose not to" are different statements, and only the second is a
position.

### Halted subscriptions with chargeable issued invoices

**The largest one, and the most annoying to leave.** A skipped invoice stays chargeable
after halt, and charging it does not consume a retry — so it is recoverable revenue at
**zero cost against the NPCI cap**. The documented baseline abandons it entirely: it
halts and stops.

Not built because it is a different system. It is a collections workflow over a static
list of chargeable invoices, not an allocation problem — there is no scarce resource to
allocate, which is the entire subject here. **[INFERRED]** that it is the largest leak;
I have no volume figure for how many halted subscriptions carry chargeable invoices.

### Mandate registration drop-off

Razorpay's own figure is ~30% of subscribers dropping off before registration completes.
Large, real, and **already covered by their Intelligent Retry Engine**, which explicitly
addresses registration drop-off. Building it would duplicate a shipped product — the
mistake CHALLENGES 001 exists to record.

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
