# DLT compliance — an open question, stated rather than resolved

**Is a payment-failure recovery nudge promotional or transactional under the TRAI
DLT framework?**

This is unresolved. Both readings are defensible, they carry materially different
obligations, and the contact policy in `allocator/decisions.py` currently assumes
one of them without having established it.

> **DRAFT.** Items marked **[INFERRED]** are my understanding of the DLT framework
> rather than something verified against TRAI's regulations or a DLT registrar's
> documentation. **I have not read the primary regulation.** The framing of the
> question is from `CLAUDE.md` "Still open"; the detail around it is reconstruction
> and should be checked by someone who has, before any of it is relied on.

---

## Why it matters here

The allocator's whole argument on ATTENTION, TERMINAL and the entire LOW row is
that a **contact costs no mandate execution**, so it is the cheap action when an
execution would be wasted. Eight of twelve cells send a contact.

If those contacts are promotional under DLT, they inherit consent and timing rules
that the current design does not model — and the cheap action stops being cheap.

---

## The two readings

### Reading A — transactional

The message is triggered by a specific transaction the customer initiated (a
mandate they authorised, a debit that failed), it concerns that transaction only,
and it is necessary for the customer to complete something they already agreed to.

**[INFERRED]** — under this reading a transactional/service-implicit classification
would typically mean:

- delivery permitted regardless of DND / preference registration
- no separate marketing consent needed
- **no time-of-day restriction**
- a registered header and a pre-approved template still required

### Reading B — promotional

The message contains a call to action that leads to a payment page. It is sent by
the merchant to recover revenue. A recovery link is, functionally, an attempt to
get the customer to transact.

**[INFERRED]** — under this reading:

- **blocked for customers on the DND / preference register**
- explicit consent required, recorded against the registered principal entity
- **time-of-day restriction — commonly cited as 09:00–21:00** *(this specific window
  is [INFERRED] and I have not verified it against current TRAI rules)*
- separate template registration under a promotional category

---

## The difference that actually bites

| | Reading A | Reading B |
|---|---|---|
| Reaches DND-registered customers | Yes | **No** |
| Consent needed | Implicit in the mandate | Explicit and separate |
| Time-of-day limit | None | **Yes** |
| Deliverable share of a batch | ~all | Unknown, possibly much less |

**The time window is the one that interacts with the design.** NPCI bars mandate
*executions* during 10:00–13:00 and 17:00–21:30 IST. If promotional messaging is
also restricted to roughly 09:00–21:00, the intersection of "may send a contact"
and "may schedule an execution" narrows considerably — and the allocator currently
schedules contacts at `now + 1 hour` with **no time-of-day check at all**.

The guard checks peak windows for executions only. `Guard._contact` checks the
contact budget, the cooldown, and that the moment is in the future. It does not
check the hour.

---

## What the code currently assumes

**Reading A, implicitly, and nowhere stated until now.**

Evidence in the code:

- `allocator/arm_c.py`, `_plan_contact`: schedules at `now + 1 hour` with a comment
  that a contact "carries no PDN obligation and no peak-hour bar: it is not a
  mandate execution." That is correct about NPCI. It is silent about DLT.
- `recovery/guard.py`, `_contact`: no time-of-day check.
- `config/default.yaml`, `allocator.contact`: has `default_channel: sms` and
  `attention_channel: whatsapp`, with no consent or preference-register field.
- Nothing anywhere models a DND / preference register at all.

So the current position is: contacts are treated as transactional, sendable at any
hour, to anyone.

---

## What would change under Reading B

**[INFERRED]** — my reading of the consequences, not a compliance assessment:

1. **A time-of-day check in the guard**, alongside the existing peak-window check
   but with a different window and applying to contacts rather than executions.
2. **A consent/preference input on `GuardRequest`**, and a new block reason for a
   customer who cannot be messaged. This is the larger change: it introduces a
   population that is *structurally uncontactable*, which the allocator's LOW row
   currently assumes away — the LOW row's argument is that a link is right whichever
   cause is true, and that argument needs the link to be deliverable.
3. **A measurable reduction in Arm B and Arm C effectiveness**, since both lean on
   contact. Arm B contacts every case; Arm C contacts on eight of twelve cells. The
   comparison would shift, and **[INFERRED]** plausibly toward Arm A, which sends
   nothing.
4. **The WhatsApp channel may sit under a different regime entirely.** WhatsApp
   Business messaging has its own template-approval and opt-in rules that are not
   the DLT framework. `attention_channel: whatsapp` may therefore be governed by two
   overlapping regimes rather than one. **[INFERRED]** and not investigated.

---

## Why it is not resolved here

Resolving it needs the primary TRAI regulation and, realistically, a DLT registrar's
categorisation guidance — the same class of primary source that is already missing
for the NPCI retry-cap circular, which `CLAUDE.md` records as cited via secondary
sources.

Guessing would be worse than stating the question. A wrong guess toward Reading A
produces a system that sends non-compliant messages; a wrong guess toward Reading B
produces one that withholds messages it was entitled to send and reports a worse
result than the policy deserves.

**The honest position is that the contact policy is built on an unverified
classification, and that this is the compliance question with the most reach into
the design.**

---

## What would settle it

- The current TRAI TCCCPR text on transactional vs service-implicit vs promotional
  categories
- A DLT registrar's template-category guidance for payment-failure notifications
- Precedent: how Razorpay's own Intelligent Retry Engine registers its WhatsApp
  recovery templates — **[INFERRED]** that this would be visible, but it is the
  cheapest available answer if it is

---

## Related open questions

Recorded in `CLAUDE.md` "Still open" and not resolved:

- NPCI primary circular text for the retry cap — cited via secondary sources
- Whether `auto_represent_on_failure` bypasses a fresh PDN for technical declines
- Whether a failed *first* presentation auto-revokes the mandate, as one PSP's docs
  reportedly claim

All four share a shape: **documented behaviour that the design depends on, sourced
secondarily or not at all.** They are listed together because a panel asking about
one will likely ask about the others.
