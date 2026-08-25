"""C4 -- the guard.

Every proposal passes through, and every block carries a reason. These assert
each check fires on its own condition, that the reason reported is the real one,
and that a block is never silently swallowed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from recovery.guard import (
    COUNTERS,
    BlockReason,
    DiscountCustomerAttempts,
    Guard,
    GuardRequest,
    ProposalKind,
    SystemInitiatedOnly,
    guard_from_config,
)
from recovery.models import PaymentStatus
from recovery.sim.calendar import IST, calendar_from_config

DECIDED = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
COMPLIANT = dt.datetime(2026, 3, 3, 3, 0, tzinfo=IST)  # +26h, non-peak


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def guard(config, calendar):
    return guard_from_config(config, calendar)


def execution(**overrides) -> GuardRequest:
    base = dict(
        kind=ProposalKind.EXECUTION,
        decided_at=DECIDED,
        execute_at=COMPLIANT,
        attempts_seen=1,
        contacts_seen=0,
        payment_status=PaymentStatus.FAILED,
        order_id="order_1",
    )
    base.update(overrides)
    return GuardRequest(**base)


def contact(**overrides) -> GuardRequest:
    base = dict(
        kind=ProposalKind.CONTACT,
        decided_at=DECIDED,
        execute_at=DECIDED + dt.timedelta(hours=1),
        attempts_seen=1,
        contacts_seen=0,
        payment_status=PaymentStatus.FAILED,
        order_id="order_1",
    )
    base.update(overrides)
    return GuardRequest(**base)


# --------------------------------------------------------------- baseline -- #


def test_a_compliant_execution_is_allowed(guard):
    assert guard.check(execution()).allowed


def test_a_compliant_contact_is_allowed(guard):
    assert guard.check(contact()).allowed


# ------------------------------------------------------------ each check -- #


def test_execution_cap(guard, config):
    verdict = guard.check(execution(attempts_seen=config.regulatory.attempt_cap))
    assert verdict.reason is BlockReason.EXECUTION_CAP_EXHAUSTED
    assert "4" in verdict.detail


def test_peak_window_morning(guard):
    verdict = guard.check(
        execution(execute_at=dt.datetime(2026, 3, 3, 11, 30, tzinfo=IST))
    )
    assert verdict.reason is BlockReason.PEAK_HOUR_BARRED


def test_peak_window_evening(guard):
    verdict = guard.check(
        execution(execute_at=dt.datetime(2026, 3, 3, 19, 0, tzinfo=IST))
    )
    assert verdict.reason is BlockReason.PEAK_HOUR_BARRED


def test_pdn_lead_time(guard):
    verdict = guard.check(
        execution(execute_at=DECIDED + dt.timedelta(hours=6))
    )
    assert verdict.reason is BlockReason.PDN_LEAD_TIME_UNMET


def test_pdn_cutoff_bites_before_a_naive_24h(guard):
    """A PDN at/after 23:50 is rejected when the debit date is T+1."""
    execute_at = dt.datetime(2026, 3, 3, 23, 55, tzinfo=IST)
    naive_deadline = execute_at - dt.timedelta(hours=24)
    verdict = guard.check(
        execution(decided_at=naive_deadline + dt.timedelta(minutes=1), execute_at=execute_at)
    )
    assert verdict.reason is BlockReason.PDN_LEAD_TIME_UNMET


def test_execution_must_be_in_the_future(guard):
    """There is no ATTEMPT_NOW."""
    verdict = guard.check(execution(execute_at=DECIDED - dt.timedelta(hours=1)))
    assert verdict.reason is BlockReason.EXECUTE_AT_NOT_IN_FUTURE


def test_execution_must_name_a_time(guard):
    assert guard.check(execution(execute_at=None)).reason is (
        BlockReason.EXECUTE_AT_NOT_IN_FUTURE
    )


def test_prior_attempt_unresolved(guard):
    """Emandate is asynchronous."""
    verdict = guard.check(
        execution(attempt_pending_until=COMPLIANT + dt.timedelta(hours=12))
    )
    assert verdict.reason is BlockReason.PRIOR_ATTEMPT_UNRESOLVED


def test_contact_budget(guard, config):
    verdict = guard.check(contact(contacts_seen=config.guard.contact_budget_per_case))
    assert verdict.reason is BlockReason.CONTACT_BUDGET_EXHAUSTED


def test_contact_cooldown(guard):
    verdict = guard.check(contact(last_contact_at=DECIDED - dt.timedelta(hours=2)))
    assert verdict.reason is BlockReason.CONTACT_COOLDOWN_ACTIVE


def test_contact_allowed_once_the_cooldown_elapses(guard, config):
    elapsed = dt.timedelta(hours=config.guard.contact_cooldown_hours + 1)
    assert guard.check(contact(last_contact_at=DECIDED - elapsed)).allowed


def test_order_expiry(guard):
    verdict = guard.check(execution(order_expires_at=DECIDED - dt.timedelta(hours=1)))
    assert verdict.reason is BlockReason.ORDER_EXPIRED


def test_order_expiry_measured_at_execution_not_decision(guard):
    """An order live now but expired by the scheduled time cannot carry it."""
    verdict = guard.check(
        execution(order_expires_at=COMPLIANT - dt.timedelta(hours=1))
    )
    assert verdict.reason is BlockReason.ORDER_EXPIRED


def test_execution_without_an_order_is_blocked(guard):
    """Attempts are clubbed by order; an execution with no order has no chain."""
    verdict = guard.check(execution(order_id=None))
    assert verdict.reason is BlockReason.ORDER_INVALID


def test_contact_without_an_order_is_permitted(guard):
    """A recovery link is not a mandate execution and needs no attempt chain."""
    assert guard.check(contact(order_id=None)).allowed


@pytest.mark.parametrize(
    "status", [PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.REFUNDED]
)
def test_payment_already_succeeded(guard, status):
    """The late-authorisation invariant at the admission point."""
    assert guard.check(execution(payment_status=status)).reason is (
        BlockReason.PAYMENT_ALREADY_SUCCEEDED
    )


def test_idempotency(guard):
    assert guard.check(execution(already_decided=True)).reason is (
        BlockReason.ALREADY_DECIDED
    )


# ------------------------------------------------------- reason ordering -- #


def test_a_settled_payment_is_blocked_for_that_and_not_something_else(guard):
    """The reason is what gets audited, so which check fires first matters."""
    verdict = guard.check(
        execution(
            payment_status=PaymentStatus.CAPTURED,
            execute_at=dt.datetime(2026, 3, 3, 11, 30, tzinfo=IST),  # also peak
            attempts_seen=9,  # also over cap
        )
    )
    assert verdict.reason is BlockReason.PAYMENT_ALREADY_SUCCEEDED


def test_every_block_carries_a_reason(guard):
    """Never silently swallowed."""
    blocked = [
        guard.check(execution(attempts_seen=9)),
        guard.check(execution(execute_at=dt.datetime(2026, 3, 3, 11, 30, tzinfo=IST))),
        guard.check(contact(contacts_seen=99)),
        guard.check(execution(order_expires_at=DECIDED - dt.timedelta(days=1))),
    ]
    for verdict in blocked:
        assert verdict.blocked
        assert verdict.reason is not None
        assert str(verdict)


# ------------------------------------------------ execution counting ------ #


def test_default_counter_trusts_attempts_seen():
    assert SystemInitiatedOnly().executions_used(3, 2) == 3


def test_pessimistic_counter_discounts_our_contacts():
    """CHALLENGES 008: order.attempts counts customer taps on a recovery link."""
    assert DiscountCustomerAttempts().executions_used(3, 2) == 1


def test_pessimistic_counter_never_goes_below_the_original_execution():
    assert DiscountCustomerAttempts().executions_used(1, 5) == 1


def test_counter_choice_changes_admission(calendar):
    """The open question is load-bearing: the two answers admit differently."""
    request = execution(attempts_seen=4, contacts_seen=2)
    strict = Guard(calendar, counter=SystemInitiatedOnly())
    lenient = Guard(calendar, counter=DiscountCustomerAttempts())

    assert strict.check(request).reason is BlockReason.EXECUTION_CAP_EXHAUSTED
    assert lenient.check(request).allowed


def test_counter_is_selected_by_config(config, calendar):
    assert set(COUNTERS) == {"system_initiated", "discount_customer_attempts"}
    built = guard_from_config(config, calendar)
    assert isinstance(built.counter, type(COUNTERS[config.guard.execution_counter]))


def test_unknown_counter_is_rejected_at_config_load(tmp_path):
    import yaml
    from pydantic import ValidationError

    from recovery.config import load_config

    data = yaml.safe_load(
        __import__("pathlib").Path("config/default.yaml").read_text(encoding="utf-8")
    )
    data["guard"]["execution_counter"] = "wishful_thinking"
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


# ------------------------------------------------- arm attribution -------- #


def test_blocked_proposals_are_attributable_per_arm(config, classifier, calendar):
    """What each arm tried, versus what it was allowed to do."""
    from recovery.sim.arms import ArmA
    from recovery.sim.environment import ActionKind, Environment, Proposal
    from recovery.sim.batch import generate_batch
    from recovery.sim.metrics import ArmMetrics
    from recovery.sim.world import load_world_config, sample_world

    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 10}
    world = sample_world(seed=5, raw=raw)
    batch = generate_batch(world)
    environment = Environment(world, batch, calendar, guard=guard_from_config(config, calendar))
    metrics = ArmMetrics(arm="probe")

    for failure in batch:
        environment.submit(
            Proposal(
                case_id=failure.case_id,
                kind=ActionKind.ATTEMPT,
                execute_at=dt.datetime(2026, 3, 4, 11, 30, tzinfo=IST),  # peak
            ),
            DECIDED,
            metrics,
        )

    assert metrics.proposals_rejected == len(batch)
    assert BlockReason.PEAK_HOUR_BARRED.value in metrics.rejection_reasons


def test_environment_uses_the_guard_it_is_given(config, calendar):
    from recovery.sim.environment import Environment
    from recovery.sim.world import sample_world

    supplied = guard_from_config(config, calendar)
    environment = Environment(sample_world(seed=1), [], calendar, guard=supplied)
    assert environment.guard is supplied


def test_a_resolved_case_is_moot_not_guard_blocked(config, classifier, calendar):
    """A multi-action arm whose first action worked was succeeding, not blocked.

    Arm B proposes a contact and a retry in the same tick. When the contact
    converts, the case closes and the retry is pointless -- but it never reaches
    the guard, so counting it as a guard block would report the arm as
    constrained by the exact action that worked.
    """
    from recovery.sim.arms import ArmB
    from recovery.sim.batch import generate_batch
    from recovery.sim.metrics import ArmMetrics
    from recovery.sim.run import run_arm
    from recovery.sim.world import load_world_config, sample_world

    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 250}
    world = sample_world(seed=42, raw=raw)

    metrics = run_arm(
        ArmB(calendar), world, calendar, costs=classifier.config.costs
    )
    assert metrics.moot_proposals > 0, "Arm B should resolve some cases mid-tick"
    assert "case_not_open" not in metrics.rejection_reasons


def test_single_action_arms_have_no_moot_proposals(config, classifier, calendar):
    """A and C emit at most one proposal per tick, so cannot collide with themselves."""
    from allocator.arm_c import ArmC
    from recovery.sim.arms import ArmA
    from recovery.sim.run import run_arm
    from recovery.sim.world import load_world_config, sample_world

    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 150}
    world = sample_world(seed=42, raw=raw)

    for arm in (ArmA(calendar), ArmC(calendar, classifier, config)):
        metrics = run_arm(arm, world, calendar, costs=classifier.config.costs)
        assert metrics.moot_proposals == 0, arm.name
