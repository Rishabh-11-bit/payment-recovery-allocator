"""Adapts the allocator to the event core's `Decider` protocol.

The worker was written against a protocol so the allocator could land later
without touching it, and `PendingAllocatorDecider` was the placeholder that
proved the seam. This is the real implementation of that seam.

**The adapter lives here, not in `recovery/`, so the dependency stays one-way.**
`allocator` already imports from `recovery`; putting the adapter in the worker
would make `recovery` import `allocator` back, and the event core is supposed to
run without an allocator at all -- that is the property the placeholder existed
to demonstrate.

### What the event core cannot tell the allocator, and why it is stated

`ArmC.plan` takes a `CaseView`, which the *simulator* builds with full knowledge
of a case's history. The event core knows less, and the gaps are declared rather
than defaulted quietly:

* `contacts_used` is **not tracked by the event core**. The simulator counts
  contacts because it owns the whole lifecycle; the live path records decisions
  and leaves contact accounting to the guard's own contact budget. Passing 0
  means the allocator's contact-budget reasoning is inert on this path -- the
  guard still enforces it, so nothing is unsafe, but the allocator is not
  making that half of the decision here.
* `last_attempt_resolved_at` is unknown, so the Emandate prior-attempt-resolved
  rule is likewise the guard's job on this path.

Both are real limitations of wiring a batch-shaped allocator to a single-event
core, and both are visible in the decision reason rather than hidden. The
demonstration this path exists for -- that a real classification produces a real
cell -- does not depend on either.
"""

from __future__ import annotations

import datetime as dt

from allocator.arm_c import ArmC, Plan
from recovery.models import Case, Classification, DecisionAction, PaymentSnapshot
from recovery.sim.environment import ActionKind, CaseOutcome, CaseView

__all__ = ["ArmCDecider", "case_view_from_snapshot"]


def case_view_from_snapshot(
    case: Case, snapshot: PaymentSnapshot, attempt_n: int
) -> CaseView:
    """Build the allocator's view from the authoritative payment snapshot.

    From the *snapshot*, never the webhook body: the late-authorisation window
    means a payload saying `failed` can describe a payment that is now
    authorized, and the allocator must not decide against a stale status.
    """
    return CaseView(
        case_id=case.case_id,
        rail=snapshot.method or "",
        amount_paise=snapshot.amount or 0,
        failed_at=case.opened_at,
        observed={
            "id": snapshot.id,
            "order_id": snapshot.order_id,
            "amount": snapshot.amount,
            "status": snapshot.status.value,
            "method": snapshot.method,
            "error_code": snapshot.error_code,
            "error_source": snapshot.error_source,
            "error_step": snapshot.error_step,
            "error_reason": snapshot.error_reason,
        },
        attempts_used=attempt_n,
        # See the module docstring: not tracked by the event core, and the guard
        # enforces the contact budget independently.
        contacts_used=0,
        outcome=CaseOutcome.OPEN,
        attempt_pending=False,
        last_attempt_resolved_at=None,
    )


class ArmCDecider:
    """`Decider` implementation backed by the real allocator.

    Returns the cell's action and a reason naming the class, the band and the
    cell's own rationale -- so `python -m recovery.explain` shows why the cell
    was chosen, not merely which one was.
    """

    def __init__(self, arm: ArmC) -> None:
        self.arm = arm

    def plan(self, case: Case, snapshot: PaymentSnapshot, attempt_n: int) -> Plan:
        view = case_view_from_snapshot(case, snapshot, attempt_n)
        return self.arm.plan(view, dt.datetime.now(dt.timezone.utc))

    def execution_slot(
        self, case: Case, snapshot: PaymentSnapshot, attempt_n: int
    ) -> dt.datetime | None:
        """When this execution would run, for the guard's admission check.

        The allocator has already chosen a compliant slot -- `compliant_slot`
        applies the non-peak window and the rail's PDN lead. Without this hook
        the worker hands the guard `execute_at=None`, and every SCHEDULE_AT is
        refused for not naming a time it had in fact already picked.
        """
        plan = self.plan(case, snapshot, attempt_n)
        for proposal in plan.proposals:
            if proposal.kind is ActionKind.ATTEMPT:
                return proposal.execute_at
        return None

    def decide(
        self,
        case: Case,
        snapshot: PaymentSnapshot,
        classification: Classification,
        attempt_n: int,
    ) -> tuple[DecisionAction, str]:
        """The classifier already ran in the worker; the allocator runs its own.

        Deliberately not reusing the passed-in `classification`. The allocator
        owns classification as part of deciding -- that is what makes it
        cause-aware rather than a consumer of someone else's label -- and
        `ArmC.plan` is the unit the simulator, the sweep and the tests all
        exercise. Re-deriving keeps this path identical to the measured one.

        The two agree by construction: same classifier instance, same key. If
        they ever diverge the reason string carries the allocator's own reading,
        which is the one that produced the action.
        """
        plan = self.plan(case, snapshot, attempt_n)
        cell = plan.cell
        reason = (
            f"{plan.classification.failure_class.value} "
            f"({plan.classification.band.value}, "
            f"confidence {plan.classification.confidence:.2f})"
        )
        if cell is not None:
            reason += (
                f" -> cell spends_execution={str(cell.spends_execution).lower()}"
                f"{f', contact={cell.contact_kind}' if cell.contact_kind else ''}"
                f": {cell.rationale}"
            )
        # Only when it says something the cell's own rationale did not -- a rail
        # policy override or a closed case. Otherwise it is the same sentence twice.
        if plan.reason and (cell is None or plan.reason != cell.rationale):
            reason += f" [{plan.reason}]"
        return plan.action, reason
