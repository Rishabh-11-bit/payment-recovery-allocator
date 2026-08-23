"""Arm C -- the allocator.

Cause-aware, budget-aware, mandate-survival-weighted. Plugs into the comparison
harness unchanged:

    from allocator.arm_c import ArmC
    run_comparison([ArmA(cal), ArmB(cal), ArmC(cal, classifier, config)], world, cal)

The policy is the twelve-cell table in `decisions.py`. This module does the
wiring around it: classify the observed payload, look up the cell, check the
budget the cell wants to spend, and turn the cell into a compliant proposal.

Two things live outside the table on purpose, because neither is settled:
`policies.RailPolicy` and `policies.ExecutionCounter`. See that module.

**Ordinal only.** Nothing here reads a probability, from config or anywhere
else. Branching is on `classification.band`, never on `classification.confidence`
compared to a number -- the thresholds are config, and treating them as policy
would put a magnitude in the decision path. `recovery/contract.py` enforces this
statically over this package.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from allocator.decisions import CARD_CHANGE, Cell, lookup
from allocator.policies import (
    ExecutionCounter,
    RailAgnosticCells,
    RailPolicy,
    SystemInitiatedOnly,
)
from allocator.scheduling import compliant_slot, next_funding_day, offset_day
from recovery.classifier import Classifier
from recovery.config import Config
from recovery.models import Classification, ConfidenceBand, DecisionAction, FailureClass
from recovery.normalize import normalize_entity
from recovery.sim.calendar import IST, ComplianceCalendar
from recovery.sim.environment import ActionKind, CaseOutcome, CaseView, Proposal

__all__ = ["ArmC", "Plan"]


@dataclass(frozen=True)
class Plan:
    """What the allocator decided, and why. The audit record of one decision.

    Separate from the proposals so a decision can be inspected and tested
    without executing anything -- including the decisions that produce no
    proposal at all, which are the ones most easily mistaken for inaction.
    """

    action: DecisionAction
    cell: Cell | None
    classification: Classification
    proposals: list[Proposal] = field(default_factory=list)
    reason: str = ""

    @property
    def spends_execution(self) -> bool:
        return any(p.kind is ActionKind.ATTEMPT for p in self.proposals)

    @property
    def surrendered(self) -> bool:
        return self.action is DecisionAction.SURRENDER


class ArmC:
    """The allocator."""

    name = "C"

    def __init__(
        self,
        calendar: ComplianceCalendar,
        classifier: Classifier,
        config: Config | None = None,
        *,
        rail_policy: RailPolicy | None = None,
        execution_counter: ExecutionCounter | None = None,
    ) -> None:
        self.calendar = calendar
        self.classifier = classifier
        self.config = config
        self.rails = rail_policy or RailAgnosticCells()
        self.counter = execution_counter or SystemInitiatedOnly()

    # ------------------------------------------------------------ classify --

    def classify(self, view: CaseView) -> Classification:
        key = normalize_entity(
            view.observed,
            source_space=self.classifier.config.source_space,
            source_aliases=self.classifier.config.source_aliases,
        )
        return self.classifier.classify(key)

    # --------------------------------------------------------------- plan ---

    def plan(self, view: CaseView, now: dt.datetime) -> Plan:
        """Decide, without executing. `propose` is a thin wrapper over this."""
        classification = self.classify(view)
        cell = lookup(classification.failure_class, classification.band)

        if view.outcome is not CaseOutcome.OPEN:
            return Plan(DecisionAction.HOLD, cell, classification, reason="case not open")

        # The rail may override the cell. Currently only under the alternative
        # policy -- see OPEN QUESTION 1.
        if self.rails.adjusts_cell(view, classification.failure_class, classification.band):
            return Plan(
                DecisionAction.SURRENDER,
                cell,
                classification,
                reason=f"rail policy overrides the cell for {view.rail}",
            )

        if cell.spends_execution:
            return self._plan_execution(view, now, cell, classification)
        return self._plan_contact(view, now, cell, classification)

    # ---------------------------------------------------------- executions --

    def _plan_execution(
        self, view: CaseView, now: dt.datetime, cell: Cell, classification: Classification
    ) -> Plan:
        used = self.counter.executions_used(view)
        if used >= self.calendar.attempt_cap:
            # SURRENDER the attempt budget. Note this does not surrender the
            # customer -- but the contact budget for this case is governed by
            # the contact cells, and an INFRASTRUCTURE/LIQUIDITY case that has
            # exhausted its executions has no contact cell to fall back to.
            return Plan(
                DecisionAction.SURRENDER,
                cell,
                classification,
                reason=f"mandate-execution budget exhausted ({used}/{self.calendar.attempt_cap})",
            )

        if not self.rails.may_attempt(view):
            # Emandate: the prior execution has not resolved. Holding is not
            # indecision, it is the only legal move.
            return Plan(
                DecisionAction.HOLD,
                cell,
                classification,
                reason="prior execution unresolved (asynchronous rail)",
            )

        target = self._target_day(view, now, classification)
        slot = compliant_slot(self.calendar, now, target)
        if slot is None:
            return Plan(
                DecisionAction.HOLD,
                cell,
                classification,
                reason="no compliant slot on the target day; reconsider tomorrow",
            )

        return Plan(
            DecisionAction.SCHEDULE_AT,
            cell,
            classification,
            proposals=[
                Proposal(
                    case_id=view.case_id,
                    kind=ActionKind.ATTEMPT,
                    execute_at=slot,
                    action=DecisionAction.SCHEDULE_AT.value,
                    note=f"{classification.failure_class.value}/{classification.band.value}: "
                    f"{cell.rationale}",
                )
            ],
            reason=cell.rationale,
        )

    def _target_day(
        self, view: CaseView, now: dt.datetime, classification: Classification
    ) -> dt.date:
        """Ordinal timing. Offsets and days-of-month, never a probability."""
        allocator = self.config.allocator if self.config is not None else None

        if classification.failure_class is FailureClass.LIQUIDITY:
            # The customer has no money today. Retrying tomorrow retries into
            # the same empty account and buys a second failure notification for
            # nothing, so hold until money is likely to have landed.
            if allocator is None:
                return offset_day(now, 2)
            start = offset_day(now, allocator.liquidity.min_offset_days)
            return next_funding_day(
                start,
                allocator.liquidity.funding_days_of_month,
                allocator.liquidity.max_wait_days,
            )

        # INFRASTRUCTURE. Confidence buys speed: a fault we are sure was
        # transient is most likely already gone, and less certainty buys more
        # time before committing one of three remaining executions.
        if allocator is None:
            return offset_day(now, 1 if classification.band is ConfidenceBand.HIGH else 2)
        days = (
            allocator.infrastructure.high_offset_days
            if classification.band is ConfidenceBand.HIGH
            else allocator.infrastructure.moderate_offset_days
        )
        return offset_day(now, days)

    # ------------------------------------------------------------ contacts --

    def _plan_contact(
        self, view: CaseView, now: dt.datetime, cell: Cell, classification: Classification
    ) -> Plan:
        limit = self.config.allocator.contact.max_per_case if self.config else 1
        if view.contacts_used >= limit:
            # The contact has been made and it did not convert. There is no
            # execution worth spending on this class, so the budget is
            # surrendered -- deliberately, and visibly.
            return Plan(
                DecisionAction.SURRENDER,
                cell,
                classification,
                reason="contact already made; no execution is worth spending on this class",
            )

        channel = self._channel(classification)
        shaping = self._shaping(classification)
        return Plan(
            cell.action,
            cell,
            classification,
            proposals=[
                Proposal(
                    case_id=view.case_id,
                    kind=ActionKind.CONTACT,
                    # A contact carries no PDN obligation and no peak-hour bar:
                    # it is not a mandate execution.
                    execute_at=now + dt.timedelta(hours=1),
                    action=cell.action.value,
                    channel=channel,
                    shaping=shaping,
                    note=f"{classification.failure_class.value}/{classification.band.value}: "
                    f"{cell.rationale}",
                )
            ],
            reason=cell.rationale,
        )

    def _channel(self, classification: Classification) -> str:
        if self.config is None:
            return "sms"
        contact = self.config.allocator.contact
        # ATTENTION means the customer was reached and did not act. Reaching
        # them the same way again repeats the one thing already known to fail.
        if (
            classification.failure_class is FailureClass.ATTENTION
            and classification.band is ConfidenceBand.HIGH
        ):
            return contact.attention_channel
        return contact.default_channel

    def _shaping(self, classification: Classification) -> str:
        """Exclusion is the HIGH-confidence case; reorder is the default.

        Excluding an instrument on a misdiagnosis makes recovery *harder* -- the
        customer is left without the method they would have used. So exclusion
        requires the band that says we are confident which instrument is
        degraded, and everything else promotes without removing.
        """
        if classification.may_exclude_instrument:
            return DecisionAction.EXCLUDE_INSTRUMENT.value
        return DecisionAction.REORDER_RAILS.value

    # -------------------------------------------------------------- arm ----

    def propose(self, view: CaseView, now: dt.datetime) -> list[Proposal]:
        return self.plan(view, now).proposals

    # ------------------------------------------------------- introspection --

    def migration_targets(self, view: CaseView) -> frozenset[str]:
        """Legal targets for a migration offer. Validates against the graph."""
        return self.rails.migration_targets(view.rail)

    def offers_migration(self, plan: Plan) -> bool:
        return any(p.action == DecisionAction.OFFER_RAIL_MIGRATION.value for p in plan.proposals)
