"""Arm C — the allocator.

**Body deliberately unwritten.** Wiring, imports and signature only; the policy
is hand-authored. Fill in `propose`.

Plugs into the existing harness unchanged:

    from allocator.arm_c import ArmC
    from recovery.sim.run import run_comparison

    run_comparison([ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier)], world, calendar)

---

## What you are handed

`propose(view, now)` is called once per open case per simulated day. Return a
list of `Proposal`; return `[]` to do nothing today.

`view` is a `CaseView` and carries only what an arm is allowed to know:

| Field | Meaning |
|---|---|
| `case_id` | The chain. Keyed on `order_id`. |
| `rail` | `card` / `upi` / `emandate`. Governs the migration graph and the retry model. |
| `amount_paise` | What is at stake on this case. |
| `failed_at` | When the original execution failed. |
| `observed` | The payment entity as the API returned it. Classify this. |
| `attempts_used` | Starts at 1 — the original execution already failed. |
| `contacts_used` | Customer-visible contacts sent so far. |
| `attempt_pending` | Emandate: a prior attempt has not yet resolved. |
| `last_attempt_resolved_at` | When it did, if it has. |

There is no true class, no recovery curve and no hazard on the view, and a test
asserts there never will be.

## What you may return

`Proposal(case_id, kind, execute_at, note)` where `kind` is `ATTEMPT` or
`CONTACT`. The `note` lands in the audit trail — say *why*, not *what*.

The environment enforces compliance and will reject a proposal that breaks it,
counting the rejection by reason. Do not treat that as a safety net: a rejected
proposal is a decision the allocator wanted to make and could not, and it shows
up as such.

## Constraints this has to satisfy

* **Mandate-execution budget.** `attempt_cap` counts *system-initiated*
  executions. A customer tapping a recovery link is not one — see CHALLENGES
  008 and "Two counters, not one" in CLAUDE.md. The current `attempts_used`
  conflates them; resolving that is part of this work.
* **No `ATTEMPT_NOW`.** Every execution is decided >=24h ahead and must land
  outside 10:00-13:00 and 17:00-21:30 IST.
* **Ordinal only.** Branch on `classification.band`, never on a raw confidence
  compared to a number, and never on a probability. `recovery/contract.py`
  checks this statically.
* **Rail migration is an offer, not a switch**, and validates against the
  directed graph. UPI and Emandate can only migrate to Card.
* **Exclusion is the HIGH-confidence case.** `classification.may_exclude_instrument`
  is the gate; reorder is the default, because excluding on a misdiagnosis makes
  recovery harder.

## Cases the fixtures will hand you

All twelve `class x band` combinations, plus five edge cases drawn from real
captures. Two worth thinking about before writing anything:

* **`terminal_instrument` vs `terminal_merchant_configuration`.** Both TERMINAL,
  both zero retry probability, different remedies. An expired card may be
  recovered by a card-change offer; `international_transaction_not_allowed`
  cannot be recovered by anything the customer does. Same class, and a contact
  is worth spending on one and wasted on the other.
* **`generic_decline`.** LOW band, no information in the payload. This will be
  one of the highest-volume keys in a real batch, so whatever it does is a large
  share of the arm's behaviour.
"""

from __future__ import annotations

import datetime as dt

from recovery.classifier import Classifier
from recovery.models import Classification, ConfidenceBand, DecisionAction, FailureClass
from recovery.normalize import normalize_entity
from recovery.sim.calendar import IST, ComplianceCalendar
from recovery.sim.environment import ActionKind, CaseOutcome, CaseView, Proposal

__all__ = ["ArmC"]


class ArmC:
    """Cause-aware, budget-aware, mandate-survival-weighted."""

    name = "C"

    def __init__(self, calendar: ComplianceCalendar, classifier: Classifier) -> None:
        self.calendar = calendar
        self.classifier = classifier

    def classify(self, view: CaseView) -> Classification:
        """Observed payload -> classification. Wired; usable as-is.

        Separate from `propose` so the decision logic can be tested against a
        supplied classification without going through the taxonomy. See
        `recovery.contract.FixedClassifier`.
        """
        key = normalize_entity(
            view.observed,
            source_space=self.classifier.config.source_space,
            source_aliases=self.classifier.config.source_aliases,
        )
        return self.classifier.classify(key)

    def propose(self, view: CaseView, now: dt.datetime) -> list[Proposal]:
        """One case, one day. Return the actions to take, or `[]`.

        YOURS TO WRITE.
        """
        raise NotImplementedError(
            "Arm C is the hand-authored allocator (C3). Implement propose() here; "
            "the harness in recovery/contract.py defines what it has to satisfy."
        )
