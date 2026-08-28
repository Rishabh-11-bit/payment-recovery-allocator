"""The two open questions, made pluggable.

Neither is settled, so neither is baked into the decision table. Each is a
strategy object with a documented default and at least one alternative, so
answering the question later is a constructor argument rather than a rewrite.

    OPEN QUESTION 1 -- does the rail change any cell?
    OPEN QUESTION 2 -- what does the attempt cap actually count? (CHALLENGES 008)
"""

from __future__ import annotations

from typing import Protocol

from recovery.models import FailureClass
from recovery.sim.environment import CaseView

# Documented migration graph. UPI and Emandate can only move to Card; Card is
# the hub. Manual charging of a domestic card is not supported, so every one of
# these is an *offer* the customer acts on, never a switch the system executes.
MIGRATION_GRAPH: dict[str, frozenset[str]] = {
    "card": frozenset({"card", "upi", "emandate"}),
    "upi": frozenset({"card"}),
    "emandate": frozenset({"card"}),
}


# --------------------------------------------------------------------------- #
# OPEN QUESTION 1: does the rail change any cell?
# --------------------------------------------------------------------------- #


class RailPolicy(Protocol):
    def may_attempt(self, view: CaseView) -> bool: ...
    def migration_targets(self, rail: str) -> frozenset[str]: ...
    def adjusts_cell(self, view: CaseView, failure_class, band) -> bool: ...


class RailAgnosticCells:
    """ASSUMPTION (default): rail does not change *which* cell fires.

    It changes two things around the cell, and neither is a policy difference:

    1. **When an attempt is permitted.** Emandate is asynchronous -- a retry is
       only allowed once the previous one has been confirmed or rejected. That
       is a timing constraint, not a different decision.
    2. **What a migration offer can name.** UPI and Emandate can only migrate to
       Card. Card can go anywhere.

    Why this is the default rather than a per-rail table: the cells are keyed on
    *why the payment failed*, and nothing about the rail changes what an expired
    card or an empty account means. A UPI mandate that fails for insufficient
    funds and a card mandate that fails for insufficient funds are the same
    problem, and the customer's balance does not care which rail asked.

    **Where this could be wrong, and it is worth saying out loud:** Card is
    frequently a worse conversion path in India than the UPI that just failed.
    So for a UPI TERMINAL failure the migration offer points at the *only* legal
    target, and that target may convert badly enough that the offer is not worth
    the contact. If that turns out to be true, TERMINAL/UPI becomes a different
    cell -- probably a plain SURRENDER -- and this class is where that lives.
    Swap in an alternative rather than editing the table.
    """

    def may_attempt(self, view: CaseView) -> bool:
        # Emandate: confirmation or rejection of the prior attempt can exceed
        # 24h, and a second execution before it lands is not permitted.
        return not view.attempt_pending

    def migration_targets(self, rail: str) -> frozenset[str]:
        return MIGRATION_GRAPH.get(rail, frozenset({"card"}))

    def adjusts_cell(self, view: CaseView, failure_class, band) -> bool:
        del view, failure_class, band
        return False


class EmandateHoldsTerminal:
    """ALTERNATIVE: Emandate TERMINAL surrenders instead of offering migration.

    The case for it: e-NACH re-registration takes days and the only legal target
    is Card. If the offer will not convert inside the horizon, the contact is
    spent for nothing and the notification still costs mandate-survival
    probability.

    Not the default because it is an empirical claim about conversion, and there
    is no source for it. Provided so the question can be tested rather than
    argued.
    """

    def __init__(self) -> None:
        self._base = RailAgnosticCells()

    def may_attempt(self, view: CaseView) -> bool:
        return self._base.may_attempt(view)

    def migration_targets(self, rail: str) -> frozenset[str]:
        return self._base.migration_targets(rail)

    def adjusts_cell(self, view: CaseView, failure_class, band) -> bool:
        del band
        return view.rail == "emandate" and failure_class is FailureClass.TERMINAL


# --------------------------------------------------------------------------- #
# OPEN QUESTION 2: what does the cap count? (CHALLENGES 008)
# --------------------------------------------------------------------------- #


class ExecutionCounter(Protocol):
    def executions_used(self, view: CaseView) -> int: ...


class SystemInitiatedOnly:
    """ASSUMPTION (default): `attempts_used` counts mandate executions only.

    True inside the simulator by construction -- `attempts_used` there increments
    on ATTEMPT proposals and never on contacts.

    **Not necessarily true in production**, which is the whole of CHALLENGES 008.
    If the counter is fed from `order.attempts`, it includes every customer tap
    on a recovery link, because a Payment Link resolves all attempts to the same
    order. That is what the five captured fixtures show: five payments, one
    order.

    Getting this wrong is asymmetric. Over-counting surrenders mandates that
    still have executions left -- invisible, and a direct loss of recoverable
    money. Under-counting breaches the NPCI cap, with API access restrictions
    behind it. Neither direction is safe, which is why the answer must be
    *recorded* rather than guessed: the system knows which attempts it initiated.
    """

    def executions_used(self, view: CaseView) -> int:
        return view.attempts_used


class DiscountCustomerAttempts:
    """ALTERNATIVE: assume the counter is conflated and subtract our contacts.

    Models the production case where `attempts_used` comes from `order.attempts`
    and therefore includes customer-initiated link payments. Subtracting the
    contacts we sent is an approximation, not a fix -- it assumes every contact
    produced at most one customer attempt, and it cannot see attempts from a
    link the customer found some other way.

    The real fix is to tag initiator at ingest. This exists so the allocator can
    be run under the pessimistic reading and the difference measured, rather
    than the question being settled by whichever default happened to be typed.
    """

    def executions_used(self, view: CaseView) -> int:
        # Never below the original execution: that one definitely happened.
        return max(1, view.attempts_used - view.contacts_used)
