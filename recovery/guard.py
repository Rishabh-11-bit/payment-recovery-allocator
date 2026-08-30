"""C4 -- the guard. Admission control between Allocate and Execute.

    Ingest -> Normalize -> Classify -> Allocate -> **Guard** -> Execute -> ...

Every proposal passes through. The allocator decides what it *wants*; the guard
decides what is *permitted*, and the two are kept apart on purpose:

* An allocator that polices itself cannot be audited against its own rules, and a
  bug in the policy silently becomes a compliance breach.
* Keeping them separate means every arm faces the same admission rules, so a
  comparison measures policy rather than which arm remembered the regulations.
* **A block is never silently swallowed.** Every one carries a reason, is
  recorded, and is attributable per arm -- so "the arm wanted to and could not"
  stays distinguishable from "the arm chose not to". That distinction is the
  difference between a policy that is too conservative and one that is being
  held back.

## The checks

| Check | Source |
|---|---|
| Mandate-execution cap of 4 | NPCI: 1 initial execution + 3 retries, ever |
| Non-peak window | NPCI: 10:00-13:00 and 17:00-21:30 IST barred |
| PDN lead time >= 24h, with the 23:50 cutoff | NPCI: the notification is a prerequisite |
| Prior attempt resolved | Emandate is asynchronous |
| Contact budget | Mandate survival: contact is not free |
| Contact cooldown | Three notices in three days is how a mandate gets cancelled |
| Order validity and expiry | An expired order cannot carry a new obligation |
| Payment not already succeeded | The late-authorisation invariant |
| Idempotency key | Exactly-once, backed by a uniqueness constraint |

## Counting executions

The cap counts *system-initiated mandate executions*. A customer tapping a
recovery link is a payment against the same order and is **not** one. See
CHALLENGES 008.

**CHALLENGES 008 is resolved by Razorpay's own documentation**: "a manual charge
attempt does not count toward the remaining retries." So the two counters are
real and the platform already distinguishes them. `auth_attempts` on the
subscription payload is authoritative -- it reads 1 on `subscription.pending`
and 4 on `subscription.halted`, which is also primary-source confirmation of the
four-attempt cap that until now was cited only secondarily.

`GuardRequest.auth_attempts` therefore wins outright when present. The counter
strategies below survive only for the case where the subscription payload is not
to hand, and are no longer a guess about which population the cap counts.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Protocol

from recovery.models import PaymentStatus
from recovery.sim.calendar import IST, ComplianceCalendar


class BlockReason(str, enum.Enum):
    EXECUTION_CAP_EXHAUSTED = "execution_cap_exhausted"
    PEAK_HOUR_BARRED = "peak_hour_barred"
    PDN_LEAD_TIME_UNMET = "pdn_lead_time_unmet"
    EXECUTE_AT_NOT_IN_FUTURE = "execute_at_not_in_future"
    PRIOR_ATTEMPT_UNRESOLVED = "prior_attempt_unresolved"
    CONTACT_BUDGET_EXHAUSTED = "contact_budget_exhausted"
    CONTACT_COOLDOWN_ACTIVE = "contact_cooldown_active"
    ORDER_EXPIRED = "order_expired"
    ORDER_INVALID = "order_invalid"
    PAYMENT_ALREADY_SUCCEEDED = "payment_already_succeeded"
    ALREADY_DECIDED = "already_decided"
    PAST_UPI_COMPLETION_DEADLINE = "past_upi_completion_deadline"
    CONCURRENT_REQUEST_IN_PROGRESS = "concurrent_request_in_progress"


class ProposalKind(str, enum.Enum):
    EXECUTION = "EXECUTION"
    CONTACT = "CONTACT"


# --------------------------------------------------------------------------- #
# Execution counting -- the CHALLENGES 008 question
# --------------------------------------------------------------------------- #


class ExecutionCounter(Protocol):
    def executions_used(self, attempts_seen: int, contacts_seen: int) -> int: ...


class SystemInitiatedOnly:
    """Default: `attempts_seen` already counts only system-initiated executions."""

    def executions_used(self, attempts_seen: int, contacts_seen: int) -> int:
        del contacts_seen
        return attempts_seen


class DiscountCustomerAttempts:
    """Pessimistic: `attempts_seen` came from `order.attempts` and is conflated.

    Subtracts our own contacts as an approximation of customer-initiated
    payments. Never below 1 -- the original execution definitely happened.
    """

    def executions_used(self, attempts_seen: int, contacts_seen: int) -> int:
        return max(1, attempts_seen - contacts_seen)


COUNTERS: dict[str, ExecutionCounter] = {
    "system_initiated": SystemInitiatedOnly(),
    "discount_customer_attempts": DiscountCustomerAttempts(),
}


# --------------------------------------------------------------------------- #
# Request and verdict
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GuardRequest:
    """Everything the guard needs. Nothing it does not.

    Deliberately a value object rather than a case handle: the guard must be
    callable from the worker, from the simulator environment and from the C7
    executor without any of them sharing a state model.
    """

    kind: ProposalKind
    decided_at: dt.datetime
    execute_at: dt.datetime | None = None
    attempts_seen: int = 1
    contacts_seen: int = 0
    last_contact_at: dt.datetime | None = None
    attempt_pending_until: dt.datetime | None = None
    payment_status: PaymentStatus = PaymentStatus.FAILED
    order_id: str | None = None
    order_expires_at: dt.datetime | None = None
    already_decided: bool = False
    # PDN lead time and the completion deadline are rail-specific: UPI 25h,
    # card 36h. The flat 24h this started with was wrong for both.
    rail: str | None = None
    # AUTHORITATIVE execution count, from `auth_attempts` on the subscription
    # payload. Razorpay's own docs settle CHALLENGES 008: "a manual charge
    # attempt does not count toward the remaining retries", and auth_attempts
    # reads 1 on subscription.pending and 4 on subscription.halted -- which is
    # also primary-source confirmation of the four-attempt cap.
    #
    # When present it wins outright: the counter heuristics exist only for the
    # case where the subscription payload is not to hand.
    auth_attempts: int | None = None
    # Razorpay rejects simultaneous operations on one token --
    # `concurrent_request_in_progress`, asking for a 60-second wait -- which
    # is a real, documented error from the token cancel/update API, not a
    # hypothetical race.
    #
    # This is NOT what protects against two workers claiming the same DB job:
    # that is `claim_jobs`'s own reclaim semantics, a different mechanism,
    # already exercised by the crash-reclaim path C7 generates. This field is
    # never set by anything in the live path -- nothing here calls a token
    # cancel/update endpoint, so nothing can receive the error this guards
    # against. Tested and correct against an input the system cannot
    # currently produce. See THREAT_MODEL.md #10.
    token_busy_until: dt.datetime | None = None


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: BlockReason | None = None
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def __str__(self) -> str:
        if self.allowed:
            return "allowed"
        return f"{self.reason.value}: {self.detail}" if self.detail else self.reason.value


ALLOWED = GuardVerdict(allowed=True)


def _block(reason: BlockReason, detail: str = "") -> GuardVerdict:
    return GuardVerdict(allowed=False, reason=reason, detail=detail)


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


class Guard:
    def __init__(
        self,
        calendar: ComplianceCalendar,
        *,
        contact_budget: int = 2,
        contact_cooldown_hours: int = 24,
        order_validity_days: int = 15,
        counter: ExecutionCounter | None = None,
    ) -> None:
        self.calendar = calendar
        self.contact_budget = contact_budget
        self.contact_cooldown_hours = contact_cooldown_hours
        self.order_validity_days = order_validity_days
        self.counter = counter or SystemInitiatedOnly()

    # ------------------------------------------------------------- checks --

    def check(self, request: GuardRequest) -> GuardVerdict:
        """Ordered cheapest-and-most-fatal first, so the reason is the real one.

        A proposal on a succeeded payment is blocked for *that*, not for a
        cooldown it also happens to violate. The reason is what gets audited and
        reported, so which check fires first is a reporting decision.
        """
        for check in (
            self._payment_settled,
            self._order,
            self._idempotency,
            self._token_busy,
            self._kind_specific,
        ):
            verdict = check(request)
            if verdict.blocked:
                return verdict
        return ALLOWED

    def _payment_settled(self, request: GuardRequest) -> GuardVerdict:
        """The late-authorisation invariant, enforced at the admission point.

        A payment marked failed can become authorized while Razorpay polls the
        bank for ~3 days, and every retry lands inside that window. Admitting a
        proposal against a settled payment is the double-charge this whole
        system exists to prevent.
        """
        if request.payment_status.is_resolved:
            return _block(
                BlockReason.PAYMENT_ALREADY_SUCCEEDED,
                f"payment is {request.payment_status.value}",
            )
        return ALLOWED

    def _order(self, request: GuardRequest) -> GuardVerdict:
        """An obligation must attach to a live order.

        Order expiry is merchant- and link-configured rather than a documented
        platform constant, so `order_expires_at` is supplied by the caller when
        known and `order_validity_days` is a configured fallback, not a claim
        about Razorpay's defaults.
        """
        if request.kind is ProposalKind.EXECUTION and not request.order_id:
            return _block(
                BlockReason.ORDER_INVALID,
                "no order id: an execution cannot be clubbed into an attempt chain",
            )
        moment = request.execute_at or request.decided_at
        if request.order_expires_at is not None and moment >= request.order_expires_at:
            return _block(
                BlockReason.ORDER_EXPIRED,
                f"order expired at {request.order_expires_at.astimezone(IST):%Y-%m-%d %H:%M} IST",
            )
        return ALLOWED

    def _idempotency(self, request: GuardRequest) -> GuardVerdict:
        """Explicit check in front of the uniqueness constraint.

        The constraint is the real enforcement -- it is race-safe and this is
        not. This exists so a duplicate is *reported* as a duplicate rather than
        surfacing as an integrity error nobody can attribute.
        """
        if request.already_decided:
            return _block(BlockReason.ALREADY_DECIDED, "an obligation already exists")
        return ALLOWED

    def _token_busy(self, request: GuardRequest) -> GuardVerdict:
        """One operation per token at a time.

        Blocked rather than queued: a refusal that says when to come back
        survives a restart, and a proposal held in memory does not.
        """
        if request.token_busy_until is None:
            return ALLOWED
        moment = request.execute_at or request.decided_at
        if moment < request.token_busy_until:
            return _block(
                BlockReason.CONCURRENT_REQUEST_IN_PROGRESS,
                f"another operation holds this token until "
                f"{request.token_busy_until.astimezone(IST):%H:%M:%S} IST",
            )
        return ALLOWED

    def _kind_specific(self, request: GuardRequest) -> GuardVerdict:
        if request.kind is ProposalKind.EXECUTION:
            return self._execution(request)
        return self._contact(request)

    def _execution(self, request: GuardRequest) -> GuardVerdict:
        used = (
            request.auth_attempts
            if request.auth_attempts is not None
            else self.counter.executions_used(request.attempts_seen, request.contacts_seen)
        )
        if used >= self.calendar.attempt_cap:
            return _block(
                BlockReason.EXECUTION_CAP_EXHAUSTED,
                f"{used}/{self.calendar.attempt_cap} mandate executions used",
            )

        if request.execute_at is None:
            return _block(
                BlockReason.EXECUTE_AT_NOT_IN_FUTURE,
                "an execution must name when it will run",
            )
        if request.execute_at <= request.decided_at:
            return _block(
                BlockReason.EXECUTE_AT_NOT_IN_FUTURE,
                "execution is not in the future; there is no ATTEMPT_NOW",
            )

        # Emandate: a retry is permitted only once the previous execution has
        # been confirmed or rejected, which can exceed 24h.
        if (
            request.attempt_pending_until is not None
            and request.execute_at < request.attempt_pending_until
        ):
            return _block(
                BlockReason.PRIOR_ATTEMPT_UNRESOLVED,
                f"prior execution unresolved until "
                f"{request.attempt_pending_until.astimezone(IST):%Y-%m-%d %H:%M} IST",
            )

        # A UPI auto-debit must complete by the deadline. 21:30-24:00 is
        # outside every peak window and still unusable, which is the kind of
        # slot a peak-only check happily admits.
        if request.rail == "upi" and self.calendar.past_upi_completion_deadline(
            request.execute_at
        ):
            return _block(
                BlockReason.PAST_UPI_COMPLETION_DEADLINE,
                f"UPI must complete by {self.calendar.upi_completion_deadline} IST; "
                f"{request.execute_at.astimezone(IST):%H:%M} is past it",
            )

        if self.calendar.is_peak(request.execute_at):
            return _block(
                BlockReason.PEAK_HOUR_BARRED,
                f"{request.execute_at.astimezone(IST):%H:%M} IST is inside a peak window",
            )

        deadline = self.calendar.pdn_deadline_for(request.execute_at, request.rail)
        if request.decided_at > deadline:
            return _block(
                BlockReason.PDN_LEAD_TIME_UNMET,
                f"pre-debit notification had to be sent by "
                f"{deadline.astimezone(IST):%Y-%m-%d %H:%M} IST "
                f"({self.calendar.pdn_lead_for(request.rail)}h lead for "
                f"{request.rail or 'default'})",
            )
        return ALLOWED

    def _contact(self, request: GuardRequest) -> GuardVerdict:
        """A contact spends no execution, but it is not free.

        Every customer-visible contact carries mandate-survival cost, so the
        budget and the cooldown are the guard's side of the same argument the
        allocator makes with SURRENDER.
        """
        if request.contacts_seen >= self.contact_budget:
            return _block(
                BlockReason.CONTACT_BUDGET_EXHAUSTED,
                f"{request.contacts_seen}/{self.contact_budget} contacts used",
            )
        if request.execute_at is not None and request.execute_at <= request.decided_at:
            return _block(
                BlockReason.EXECUTE_AT_NOT_IN_FUTURE, "contact is not in the future"
            )
        if request.last_contact_at is not None:
            moment = request.execute_at or request.decided_at
            elapsed = moment - request.last_contact_at
            if elapsed < dt.timedelta(hours=self.contact_cooldown_hours):
                return _block(
                    BlockReason.CONTACT_COOLDOWN_ACTIVE,
                    f"{elapsed.total_seconds() / 3600:.1f}h since the last contact, "
                    f"cooldown is {self.contact_cooldown_hours}h",
                )
        return ALLOWED

    # -------------------------------------------------------------- helper --

    def order_expiry_for(self, created_at: dt.datetime) -> dt.datetime:
        """Configured fallback where the order entity does not carry an expiry."""
        return created_at + dt.timedelta(days=self.order_validity_days)


def guard_from_config(config, calendar: ComplianceCalendar) -> Guard:
    """Build from the `guard:` block. Every limit is config, none is hardcoded."""
    return Guard(
        calendar,
        contact_budget=config.guard.contact_budget_per_case,
        contact_cooldown_hours=config.guard.contact_cooldown_hours,
        order_validity_days=config.guard.order_validity_days,
        counter=COUNTERS[config.guard.execution_counter],
    )
