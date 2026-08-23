"""The decision table. Twelve cells, one lookup, no arithmetic.

This module is the policy. It is deliberately a table rather than a scoring
function: a panel can read twelve cells and disagree with a specific one, which
is not true of a weighted sum whose behaviour has to be inferred from its
outputs.

Nothing here reads a probability, from config or anywhere else. The cells depend
on orderings -- "later than HIGH would", "not before funding" -- and on
definitional facts -- "a retry cannot recover an expired card".
"""

from __future__ import annotations

from dataclasses import dataclass

from recovery.models import ConfidenceBand, DecisionAction, FailureClass


@dataclass(frozen=True)
class Cell:
    """One cell of the table.

    `spends_execution` is the load-bearing field. It is not derived from the
    action -- it is stated, because the whole point of the table is that some
    recovery actions cost a capped mandate execution and some cost nothing.
    """

    action: DecisionAction
    spends_execution: bool
    rationale: str
    # Set where the action is a contact, to distinguish a card-change offer from
    # a generic recovery link. Same cost, different content.
    contact_kind: str | None = None


GENERIC_LINK = "recovery_link"
CARD_CHANGE = "card_change_offer"
DIFFERENT_CHANNEL = "recovery_link_alternate_channel"


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

CELLS: dict[tuple[FailureClass, ConfidenceBand], Cell] = {
    # --- INFRASTRUCTURE: the failure was ours or the rail's, not the customer's.
    # Nothing about the customer needs to change, so the attempt is worth
    # spending. Confidence buys speed: a transient fault is most likely already
    # gone by tomorrow, and less certainty buys more time before committing one
    # of three remaining executions.
    (FailureClass.INFRASTRUCTURE, ConfidenceBand.HIGH): Cell(
        action=DecisionAction.SCHEDULE_AT,
        spends_execution=True,
        rationale="transient fault, likely already cleared; retry at the earliest compliant slot",
    ),
    (FailureClass.INFRASTRUCTURE, ConfidenceBand.MODERATE): Cell(
        action=DecisionAction.SCHEDULE_AT,
        spends_execution=True,
        rationale="probably transient; wait longer than HIGH would before spending an execution",
    ),
    # --- LIQUIDITY: the customer has no money today. The instrument works, the
    # mandate works, the balance is the problem -- so the attempt is worth
    # spending, but *when* is the entire decision. Retrying tomorrow retries into
    # the same empty account and buys a second failure notification.
    (FailureClass.LIQUIDITY, ConfidenceBand.HIGH): Cell(
        action=DecisionAction.SCHEDULE_AT,
        spends_execution=True,
        rationale="no funds today; hold the execution until money is likely to have landed",
    ),
    (FailureClass.LIQUIDITY, ConfidenceBand.MODERATE): Cell(
        action=DecisionAction.SCHEDULE_AT,
        spends_execution=True,
        rationale="probably a funding gap; same timing logic, same execution spend",
    ),
    # --- ATTENTION: the customer was reached and did not act. A retry re-runs
    # the exact interaction they already declined, so it has ~zero marginal
    # value and costs one of three executions plus a notification. The gap is
    # attention, and attention is bought with a contact, not an execution.
    (FailureClass.ATTENTION, ConfidenceBand.HIGH): Cell(
        action=DecisionAction.RECOVERY_LINK,
        spends_execution=False,
        rationale="reached, did not act; re-running the same prompt is the one thing known to fail",
        contact_kind=DIFFERENT_CHANNEL,
    ),
    (FailureClass.ATTENTION, ConfidenceBand.MODERATE): Cell(
        action=DecisionAction.RECOVERY_LINK,
        spends_execution=False,
        rationale="probably an attention gap; a link costs no execution, so the downside is small",
        contact_kind=GENERIC_LINK,
    ),
    # --- TERMINAL: P(retry succeeds) = 0, by definition of the failure. Not
    # low -- zero. Every execution spent here is spent on an outcome that cannot
    # occur, and buys a failure notification that raises revocation hazard for
    # nothing. But the customer can still act: a new card can be entered.
    (FailureClass.TERMINAL, ConfidenceBand.HIGH): Cell(
        action=DecisionAction.OFFER_RAIL_MIGRATION,
        spends_execution=False,
        rationale="retry cannot succeed by definition; the only path is a customer-entered instrument",
        contact_kind=CARD_CHANGE,
    ),
    (FailureClass.TERMINAL, ConfidenceBand.MODERATE): Cell(
        action=DecisionAction.OFFER_RAIL_MIGRATION,
        spends_execution=False,
        rationale="probably unrecoverable by retry; an offer costs no execution and can still convert",
        contact_kind=CARD_CHANGE,
    ),
    # --- LOW: see PRINCIPLE 1 at the dispatch site. Uniform on purpose.
    **{
        (failure_class, ConfidenceBand.LOW): Cell(
            action=DecisionAction.RECOVERY_LINK,
            spends_execution=False,
            rationale="class is itself a guess; take the action that is right whichever cause is true",
            contact_kind=GENERIC_LINK,
        )
        for failure_class in FailureClass
    },
}


def lookup(failure_class: FailureClass, band: ConfidenceBand) -> Cell:
    """The dispatch site. Two principles govern the shape of the table above.

    ------------------------------------------------------------------------
    PRINCIPLE 1 -- the LOW row is uniform, and that is a decision, not laziness.

    At LOW confidence the class label is itself a guess. Acting on a guess by
    spending an execution is the expensive mistake: if the truth is TERMINAL the
    attempt is guaranteed waste, and it still buys a failure notification that
    raises the probability the customer revokes the mandate. The downside is not
    "we wasted a retry", it is "we wasted a retry and moved the customer closer
    to cancelling".

    A recovery link costs zero executions -- a customer-initiated payment is not
    a mandate execution -- and it is the correct action under all four causes at
    once:

        bank outage        -> they pay now, the rail is fine
        no funds           -> they pay when funded, on their own timing
        missed notification-> the link is the notification, and reaches them
        dead card          -> they enter a new instrument

    One action, right regardless of which cause is true. That is what makes it
    the correct response to *not knowing*, rather than a hedge.

    ------------------------------------------------------------------------
    PRINCIPLE 2 -- "don't retry" and "don't contact" are separate decisions.

    They are routinely collapsed into one, and TERMINAL is where the collapse
    does damage. Retry probability on an expired card is hard zero. Card-change
    conversion is not zero -- the customer can enter a new instrument, and a
    great many will if asked in a way they can act on.

    So SURRENDER surrenders the *attempt budget*, not the *contact*. Giving up
    on the execution and giving up on the customer are different moves, and only
    the first one is forced.

    This is also where the documented baseline loses most, and it is not only
    about the three wasted attempts. It is that those three attempts generate
    three failure notices -- "your payment failed", with nothing to do about it
    -- where one card-change offer would have cost the same contact budget and
    been actionable. Same cost, entirely different value.
    ------------------------------------------------------------------------
    """
    return CELLS[(failure_class, band)]


def table_rows() -> list[tuple[str, str, str, bool, str]]:
    """Flat rendering, for reports and for reading the policy out loud."""
    return [
        (
            failure_class.value,
            band.value,
            CELLS[(failure_class, band)].action.value,
            CELLS[(failure_class, band)].spends_execution,
            CELLS[(failure_class, band)].rationale,
        )
        for failure_class in FailureClass
        for band in ConfidenceBand
    ]
