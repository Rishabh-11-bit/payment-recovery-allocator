"""The environment: holds the truth, enforces the rules, resolves outcomes.

An arm proposes; the environment disposes. Three reasons it is built this way:

1. **Regulation is a property of the world.** Peak-hour bans, PDN lead time and
   the attempt cap are enforced here, so an arm cannot benefit from ignoring
   them. Rejections are counted and attributed.
2. **Ground truth stays out of reach.** Arms receive `observed()` payloads.
   Recovery probabilities, the true class, and the revocation hazard live here.
3. **Arms are comparable.** Every arm faces the same world, same batch, same
   seeded draws, so a difference between arms is a difference in policy rather
   than in luck.

The simulated day is a loop, not a schedule, because Emandate is asynchronous:
its next retry is permitted only once the previous attempt has been confirmed or
rejected, which can exceed 24h. An arm that plans its whole schedule up front
cannot express that.
"""

from __future__ import annotations

import datetime as dt
import enum
import random
from dataclasses import dataclass, field

from recovery.classifier import CostModel
from recovery.guard import Guard, GuardRequest, ProposalKind
from recovery.models import PaymentStatus
from recovery.sim.batch import SyntheticFailure
from recovery.sim.calendar import IST, ComplianceCalendar
from recovery.sim.metrics import ArmMetrics
from recovery.sim.world import World


class ActionKind(str, enum.Enum):
    ATTEMPT = "ATTEMPT"
    CONTACT = "CONTACT"


class CaseOutcome(str, enum.Enum):
    """Exit doors, and they are not equivalent.

    HALTED preserves mandate authority and leaves the skipped invoice
    chargeable. PAUSED preserves the mandate but suspends billing, and who
    initiated it decides whether the merchant can undo it. REVOKED destroys the
    authority. EXPIRED is the subscription reaching its natural end -- not a
    failure at all, and counting it as one would inflate every loss figure.
    """

    OPEN = "open"
    RECOVERED = "recovered"
    HALTED = "halted"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Proposal:
    """What an arm asks for. Kept minimal: arms do not schedule, they request.

    `action`, `channel` and `shaping` are descriptive metadata for the audit
    trail and for reporting. **The environment does not price them.** A
    card-change offer and a generic recovery link are both one CONTACT and
    convert at the same class-dependent rate.

    That is a deliberate, conservative choice. Modelling a card-change offer as
    converting better than a generic link on a dead card is almost certainly
    true, but it would be a new invented cardinal that happens to favour the
    arm proposing it -- exactly the failure recorded in CHALLENGES 002. Until
    there is a source for the uplift, Arm C's advantage on TERMINAL is measured
    only as attempts and notifications saved, which is definitional. This
    understates Arm C rather than flattering it.
    """

    case_id: str
    kind: ActionKind
    execute_at: dt.datetime
    note: str = ""
    action: str | None = None
    channel: str | None = None
    shaping: str | None = None


@dataclass
class CaseView:
    """Everything an arm is permitted to know about a case.

    Constructed by the environment. No `true_class`, no recovery curve, no
    hazard -- if a field would let an arm read the answer, it is not here.
    """

    case_id: str
    rail: str
    amount_paise: int
    failed_at: dt.datetime
    observed: dict
    attempts_used: int
    contacts_used: int
    outcome: CaseOutcome
    attempt_pending: bool
    last_attempt_resolved_at: dt.datetime | None


@dataclass
class _CaseState:
    failure: SyntheticFailure
    # Starts at 1, not 0: the original execution has already happened and
    # failed -- that is what put this case in the batch. The NPCI cap is 1
    # initial execution plus 3 retries, so 3 attempts remain, which is exactly
    # the T+1 / T+2 / T+3 the documented baseline spends.
    attempts_used: int = 1
    contacts_used: int = 0
    notifications: int = 0
    outcome: CaseOutcome = CaseOutcome.OPEN
    attempt_pending_until: dt.datetime | None = None
    last_attempt_resolved_at: dt.datetime | None = None
    last_contact_at: dt.datetime | None = None
    order_expires_at: dt.datetime | None = None
    resolved_on_day: int | None = None
    history: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.outcome is CaseOutcome.OPEN


class Environment:
    def __init__(
        self,
        world: World,
        failures: list[SyntheticFailure],
        calendar: ComplianceCalendar,
        costs: CostModel | None = None,
        *,
        seed_offset: int = 0,
        guard: Guard | None = None,
    ) -> None:
        self.world = world
        self.calendar = calendar
        self.costs = costs
        # Admission control is C4's, not the environment's. The environment
        # resolves outcomes; the guard decides what is permitted to happen, and
        # every arm faces the same instance.
        self.guard = guard or Guard(calendar)
        # Per-arm RNG derived from the world seed: every arm gets the same draws
        # for the same case, so arms differ by policy and not by luck.
        self._rng = random.Random(world.seed * 104729 + seed_offset)
        self.cases: dict[str, _CaseState] = {
            failure.case_id: _CaseState(failure=failure) for failure in failures
        }

    # ------------------------------------------------------------- views ----

    def view(self, case_id: str, now: dt.datetime) -> CaseView:
        state = self.cases[case_id]
        return CaseView(
            case_id=case_id,
            rail=state.failure.rail,
            amount_paise=state.failure.amount_paise,
            failed_at=state.failure.failed_at,
            observed=state.failure.observed(),
            attempts_used=state.attempts_used,
            contacts_used=state.contacts_used,
            outcome=state.outcome,
            attempt_pending=state.attempt_pending_until is not None
            and now < state.attempt_pending_until,
            last_attempt_resolved_at=state.last_attempt_resolved_at,
        )

    def open_case_ids(self) -> list[str]:
        return [case_id for case_id, state in self.cases.items() if state.is_open]

    # ------------------------------------------------------------ submit ----

    def submit(
        self, proposal: Proposal, decided_at: dt.datetime, metrics: ArmMetrics
    ) -> bool:
        """Validate and, if permitted, execute. Returns True if it went through.

        A rejection is never silently swallowed: it is counted and attributed by
        reason, so "the arm wanted to but could not" stays distinguishable from
        "the arm chose not to".
        """
        state = self.cases[proposal.case_id]
        if not state.is_open:
            # Not a guard block -- this never reaches the guard. The case
            # resolved between the arm proposing and the proposal being
            # submitted, which for a multi-action arm usually means its own
            # earlier action worked.
            metrics.record_moot()
            return False

        if proposal.kind is ActionKind.ATTEMPT:
            return self._submit_attempt(state, proposal, decided_at, metrics)
        return self._submit_contact(state, proposal, decided_at, metrics)

    def _guard_request(
        self, state: _CaseState, proposal: Proposal, decided_at: dt.datetime
    ) -> GuardRequest:
        return GuardRequest(
            kind=(
                ProposalKind.EXECUTION
                if proposal.kind is ActionKind.ATTEMPT
                else ProposalKind.CONTACT
            ),
            decided_at=decided_at,
            execute_at=proposal.execute_at,
            attempts_seen=state.attempts_used,
            contacts_seen=state.contacts_used,
            last_contact_at=state.last_contact_at,
            attempt_pending_until=state.attempt_pending_until,
            payment_status=PaymentStatus.FAILED,
            order_id=state.failure.order_id,
            order_expires_at=state.order_expires_at,
        )

    def _submit_attempt(
        self,
        state: _CaseState,
        proposal: Proposal,
        decided_at: dt.datetime,
        metrics: ArmMetrics,
    ) -> bool:
        verdict = self.guard.check(self._guard_request(state, proposal, decided_at))
        if verdict.blocked:
            # Never silently swallowed: attributed by reason, per arm.
            metrics.record_rejection(str(verdict))
            return False

        state.attempts_used += 1
        metrics.record_attempt(state.failure.true_class)

        days_since = max(1, (proposal.execute_at - state.failure.failed_at).days)
        curve = self.world.recovery[state.failure.true_class]
        succeeded = self._rng.random() < curve.probability(days_since)

        if succeeded:
            self._recover(state, metrics, proposal.execute_at, via="attempt")
            return True

        # A failed attempt is customer-visible: the PDN went out, and the debit
        # failed. That is a notification, and notifications are what cost
        # mandates.
        state.history.append(f"attempt_failed@{proposal.execute_at.isoformat()}")
        self._notify(state, metrics, proposal.execute_at)

        if state.failure.rail == "emandate":
            # Confirmation or rejection can exceed 24h.
            state.attempt_pending_until = proposal.execute_at + dt.timedelta(
                hours=self._rng.uniform(24, 60)
            )
        else:
            state.attempt_pending_until = proposal.execute_at
        state.last_attempt_resolved_at = state.attempt_pending_until
        return True

    def _submit_contact(
        self,
        state: _CaseState,
        proposal: Proposal,
        decided_at: dt.datetime,
        metrics: ArmMetrics,
    ) -> bool:
        verdict = self.guard.check(self._guard_request(state, proposal, decided_at))
        if verdict.blocked:
            metrics.record_rejection(str(verdict))
            return False

        state.last_contact_at = proposal.execute_at
        cost = (
            self.costs.contact_cost(state.failure.true_class) if self.costs is not None else 0.0
        )
        state.contacts_used += 1
        metrics.record_contact(state.failure.true_class, cost)

        converted = self._rng.random() < self.world.link_conversion[state.failure.true_class]
        if converted:
            self._recover(state, metrics, proposal.execute_at, via="link")
            return True

        state.history.append(f"contact_ignored@{proposal.execute_at.isoformat()}")
        self._notify(state, metrics, proposal.execute_at)
        return True

    # ----------------------------------------------------------- outcomes ---

    def _recover(
        self, state: _CaseState, metrics: ArmMetrics, moment: dt.datetime, *, via: str
    ) -> None:
        state.outcome = CaseOutcome.RECOVERED
        state.history.append(f"recovered_via_{via}@{moment.isoformat()}")
        metrics.money_recovered_paise += state.failure.amount_paise
        metrics.cases_recovered += 1

    def _notify(self, state: _CaseState, metrics: ArmMetrics, moment: dt.datetime) -> None:
        """One customer-visible failure notification, with its survival cost.

        Ordinal content: repeated notifications raise revocation probability and
        the increase compounds. The magnitude is a swept world parameter and no
        result may be quoted at a single point in its range.
        """
        state.notifications += 1
        hazard = self.world.revocation_hazard(
            state.failure.true_class, state.notifications, state.failure.rail
        )
        if self._rng.random() < hazard:
            state.outcome = CaseOutcome.REVOKED
            state.history.append(f"mandate_revoked@{moment.isoformat()}")

    def finalise(self, metrics: ArmMetrics) -> None:
        """Close the books. Everything still open at the horizon is halted."""
        for state in self.cases.values():
            metrics.cases += 1
            metrics.money_at_risk_paise += state.failure.amount_paise

            if state.outcome is CaseOutcome.OPEN:
                # Exhausted its budget or ran out of horizon without recovering.
                state.outcome = CaseOutcome.HALTED

            if state.outcome is CaseOutcome.REVOKED:
                metrics.mandates_revoked += 1
            elif state.outcome is CaseOutcome.HALTED:
                # `halted` is the state we are actually avoiding: it needs manual
                # intervention to recover. The mandate is not revoked, but it is
                # not working either.
                metrics.mandates_halted += 1
                metrics.mandates_preserved += 1
            else:
                metrics.mandates_preserved += 1

    def exhaust_budget(self, case_id: str) -> None:
        """Mark a case as done with its attempt budget (arm-initiated stop)."""
        state = self.cases[case_id]
        if state.is_open:
            state.outcome = CaseOutcome.HALTED
            state.history.append("halted")


def default_start(world: World) -> dt.datetime:
    del world
    return dt.datetime(2026, 3, 2, 3, 0, tzinfo=IST)
