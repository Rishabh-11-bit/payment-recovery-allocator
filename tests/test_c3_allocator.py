"""Arm C -- the twelve cells, the budget, and the two open questions.

`test_c3_allocator_contract.py` asserts the allocator satisfies the *contract*
(stays in budget, answers every combination, reads no magnitudes). This file
asserts it implements the *table* -- which specific action fires in which cell,
and what that action costs.

The expected table below is the authored policy transcribed. If a cell here and
a cell in `allocator/decisions.py` disagree, this file is right and the code is
wrong.
"""

from __future__ import annotations

import datetime as dt

import pytest

from allocator.arm_c import ArmC
from allocator.decisions import CARD_CHANGE, DIFFERENT_CHANNEL, GENERIC_LINK, lookup
from allocator.policies import (
    MIGRATION_GRAPH,
    DiscountCustomerAttempts,
    EmandateHoldsTerminal,
    SystemInitiatedOnly,
)
from recovery.contract import (
    FixedClassifier,
    assert_within_execution_budget,
    classification_grid,
    drive,
    make_case_view,
)
from recovery.models import ConfidenceBand, DecisionAction, FailureClass
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import IST, calendar_from_config
from recovery.sim.environment import ActionKind, CaseOutcome
from recovery.sim.run import run_comparison
from recovery.sim.world import load_world_config, sample_world

NOW = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)

# The authored decision table, transcribed. (action, spends_execution)
EXPECTED = {
    ("INFRASTRUCTURE", "HIGH"): (DecisionAction.SCHEDULE_AT, True),
    ("INFRASTRUCTURE", "MODERATE"): (DecisionAction.SCHEDULE_AT, True),
    ("INFRASTRUCTURE", "LOW"): (DecisionAction.RECOVERY_LINK, False),
    ("LIQUIDITY", "HIGH"): (DecisionAction.SCHEDULE_AT, True),
    ("LIQUIDITY", "MODERATE"): (DecisionAction.SCHEDULE_AT, True),
    ("LIQUIDITY", "LOW"): (DecisionAction.RECOVERY_LINK, False),
    ("ATTENTION", "HIGH"): (DecisionAction.RECOVERY_LINK, False),
    ("ATTENTION", "MODERATE"): (DecisionAction.RECOVERY_LINK, False),
    ("ATTENTION", "LOW"): (DecisionAction.RECOVERY_LINK, False),
    ("TERMINAL", "HIGH"): (DecisionAction.OFFER_RAIL_MIGRATION, False),
    ("TERMINAL", "MODERATE"): (DecisionAction.OFFER_RAIL_MIGRATION, False),
    ("TERMINAL", "LOW"): (DecisionAction.RECOVERY_LINK, False),
}


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def small_world():
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 60}
    return sample_world(seed=7, raw=raw)


def arm_for(calendar, classifier, config, cell_key, **kwargs) -> ArmC:
    """An arm pinned to one cell of the table."""
    grid = classification_grid(classifier)
    return ArmC(
        calendar, FixedClassifier(grid[cell_key], classifier.config), config, **kwargs
    )


# --------------------------------------------------------------------------- #
# The twelve cells
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cell_key, expected", sorted(EXPECTED.items()))
def test_cell_fires_the_authored_action(calendar, classifier, config, cell_key, expected):
    expected_action, expected_spend = expected
    plan = arm_for(calendar, classifier, config, cell_key).plan(make_case_view(), NOW)

    assert plan.action is expected_action, f"{cell_key}: wrong action"
    assert plan.spends_execution is expected_spend, f"{cell_key}: wrong execution spend"


@pytest.mark.parametrize("cell_key, expected", sorted(EXPECTED.items()))
def test_cell_spend_matches_the_proposal_kind(calendar, classifier, config, cell_key, expected):
    """A cell that says it spends nothing must not emit an ATTEMPT."""
    _, expected_spend = expected
    plan = arm_for(calendar, classifier, config, cell_key).plan(make_case_view(), NOW)
    attempts = [p for p in plan.proposals if p.kind is ActionKind.ATTEMPT]

    assert bool(attempts) is expected_spend
    if not expected_spend:
        assert plan.proposals, f"{cell_key} must still act -- doing nothing is not the cell"


def test_only_four_of_twelve_cells_spend_an_execution():
    """Eight of twelve act without touching the capped budget."""
    spending = [key for key, (_, spends) in EXPECTED.items() if spends]
    assert len(spending) == 4
    assert {key[0] for key in spending} == {"INFRASTRUCTURE", "LIQUIDITY"}


# --------------------------------------------------------------------------- #
# Principle 1: the LOW row is uniform
# --------------------------------------------------------------------------- #


def test_low_row_is_identical_across_all_four_classes(calendar, classifier, config):
    """At LOW the class is a guess, so the action must not depend on the guess."""
    plans = {
        failure_class.value: arm_for(
            calendar, classifier, config, (failure_class.value, "LOW")
        ).plan(make_case_view(), NOW)
        for failure_class in FailureClass
    }

    actions = {plan.action for plan in plans.values()}
    kinds = {p.kind for plan in plans.values() for p in plan.proposals}
    channels = {p.channel for plan in plans.values() for p in plan.proposals}

    assert actions == {DecisionAction.RECOVERY_LINK}
    assert kinds == {ActionKind.CONTACT}
    assert len(channels) == 1


def test_low_row_never_spends_an_execution(calendar, classifier, config):
    """The expensive mistake: acting on a guess that might be TERMINAL."""
    for failure_class in FailureClass:
        plan = arm_for(
            calendar, classifier, config, (failure_class.value, "LOW")
        ).plan(make_case_view(), NOW)
        assert not plan.spends_execution


def test_low_row_never_excludes_an_instrument(calendar, classifier, config):
    """Exclusion on a diagnosis we do not trust makes recovery harder."""
    for failure_class in FailureClass:
        plan = arm_for(
            calendar, classifier, config, (failure_class.value, "LOW")
        ).plan(make_case_view(), NOW)
        assert all(
            p.shaping == DecisionAction.REORDER_RAILS.value for p in plan.proposals
        )


# --------------------------------------------------------------------------- #
# Principle 2: not retrying and not contacting are separate decisions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("band", ["HIGH", "MODERATE"])
def test_terminal_contacts_without_retrying(calendar, classifier, config, band):
    """Retry probability is zero; card-change conversion is not."""
    plan = arm_for(calendar, classifier, config, ("TERMINAL", band)).plan(
        make_case_view(), NOW
    )

    assert not [p for p in plan.proposals if p.kind is ActionKind.ATTEMPT]
    assert [p for p in plan.proposals if p.kind is ActionKind.CONTACT]
    assert lookup(FailureClass.TERMINAL, ConfidenceBand[band]).contact_kind == CARD_CHANGE


def test_surrender_gives_up_the_budget_not_the_customer(calendar, classifier, config):
    """SURRENDER surrenders the attempt budget. The contact still goes out."""
    arm = arm_for(calendar, classifier, config, ("TERMINAL", "HIGH"))

    first = arm.plan(make_case_view(contacts_used=0), NOW)
    assert first.proposals and not first.spends_execution

    # Once the contact is spent there is nothing left worth doing.
    second = arm.plan(make_case_view(contacts_used=1), NOW)
    assert second.action is DecisionAction.SURRENDER
    assert not second.proposals


def test_terminal_costs_the_same_contact_as_the_baseline_but_says_something_useful(
    calendar, classifier, config
):
    """One actionable offer where the baseline sends three dead failure notices."""
    plan = arm_for(calendar, classifier, config, ("TERMINAL", "HIGH")).plan(
        make_case_view(), NOW
    )
    contact = plan.proposals[0]

    assert contact.action == DecisionAction.OFFER_RAIL_MIGRATION.value
    assert contact.kind is ActionKind.CONTACT


# --------------------------------------------------------------------------- #
# Timing is ordinal
# --------------------------------------------------------------------------- #


def test_moderate_infrastructure_waits_longer_than_high(calendar, classifier, config):
    """Less certainty buys more time before committing a capped execution."""
    high = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(), NOW
    )
    moderate = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "MODERATE")).plan(
        make_case_view(), NOW
    )

    assert moderate.proposals[0].execute_at > high.proposals[0].execute_at


def test_liquidity_never_retries_sooner_than_infrastructure(calendar, classifier, config):
    """Retrying into the same empty account buys a second failure notice."""
    infra = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(), NOW
    )
    liquidity = arm_for(calendar, classifier, config, ("LIQUIDITY", "HIGH")).plan(
        make_case_view(), NOW
    )

    assert liquidity.proposals[0].execute_at > infra.proposals[0].execute_at


def test_liquidity_respects_the_minimum_offset(calendar, classifier, config):
    plan = arm_for(calendar, classifier, config, ("LIQUIDITY", "HIGH")).plan(
        make_case_view(), NOW
    )
    earliest = NOW + dt.timedelta(days=config.allocator.liquidity.min_offset_days)
    assert plan.proposals[0].execute_at >= earliest.replace(hour=0, minute=0)


def test_liquidity_targets_a_funding_day_when_one_is_reachable(
    calendar, classifier, config
):
    """Late in the month, month-end is inside the wait window."""
    late = dt.datetime(2026, 3, 24, 1, 0, tzinfo=IST)
    plan = arm_for(calendar, classifier, config, ("LIQUIDITY", "HIGH")).plan(
        make_case_view(failed_at=late), late
    )
    assert (
        plan.proposals[0].execute_at.astimezone(IST).day
        in config.allocator.liquidity.funding_days_of_month
    )


def test_timing_comes_from_config_not_from_code(calendar, classifier, config):
    """The comparison must re-run with changed parameters in seconds."""
    slower = config.model_copy(
        update={
            "allocator": config.allocator.model_copy(
                update={
                    "infrastructure": config.allocator.infrastructure.model_copy(
                        update={"high_offset_days": 5}
                    )
                }
            )
        }
    )
    default = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(), NOW
    )
    tuned = arm_for(calendar, classifier, slower, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(), NOW
    )

    assert tuned.proposals[0].execute_at > default.proposals[0].execute_at


# --------------------------------------------------------------------------- #
# Confidence gating
# --------------------------------------------------------------------------- #


def test_exclusion_requires_the_high_band(calendar, classifier, config):
    high = arm_for(calendar, classifier, config, ("ATTENTION", "HIGH")).plan(
        make_case_view(), NOW
    )
    moderate = arm_for(calendar, classifier, config, ("ATTENTION", "MODERATE")).plan(
        make_case_view(), NOW
    )

    assert high.proposals[0].shaping == DecisionAction.EXCLUDE_INSTRUMENT.value
    assert moderate.proposals[0].shaping == DecisionAction.REORDER_RAILS.value


def test_attention_high_switches_channel(calendar, classifier, config):
    """They were reached and did not act; the same channel is known to fail."""
    plan = arm_for(calendar, classifier, config, ("ATTENTION", "HIGH")).plan(
        make_case_view(), NOW
    )
    assert plan.proposals[0].channel == config.allocator.contact.attention_channel
    assert plan.proposals[0].channel != config.allocator.contact.default_channel
    assert lookup(FailureClass.ATTENTION, ConfidenceBand.HIGH).contact_kind == (
        DIFFERENT_CHANNEL
    )


def test_attention_moderate_uses_the_default_channel(calendar, classifier, config):
    plan = arm_for(calendar, classifier, config, ("ATTENTION", "MODERATE")).plan(
        make_case_view(), NOW
    )
    assert plan.proposals[0].channel == config.allocator.contact.default_channel
    assert lookup(FailureClass.ATTENTION, ConfidenceBand.MODERATE).contact_kind == (
        GENERIC_LINK
    )


# --------------------------------------------------------------------------- #
# Budget and compliance
# --------------------------------------------------------------------------- #


def test_surrenders_when_the_execution_budget_is_exhausted(calendar, classifier, config):
    plan = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(attempts_used=calendar.attempt_cap), NOW
    )
    assert plan.action is DecisionAction.SURRENDER
    assert not plan.proposals
    assert "budget exhausted" in plan.reason


def test_never_exceeds_the_execution_budget(calendar, classifier, config, small_world):
    arm = ArmC(calendar, classifier, config)
    assert_within_execution_budget(arm, small_world, calendar)


def test_no_proposal_lands_in_a_peak_window(calendar, classifier, config, small_world):
    arm = ArmC(calendar, classifier, config)
    attempts = [
        proposal
        for _, proposal in drive(arm, small_world, calendar)
        if proposal.kind is ActionKind.ATTEMPT
    ]
    assert attempts
    assert not [p for p in attempts if calendar.is_peak(p.execute_at)]


def test_every_attempt_clears_the_pdn_lead_time(calendar, classifier, config, small_world):
    arm = ArmC(calendar, classifier, config)
    for view, proposal in drive(arm, small_world, calendar):
        if proposal.kind is ActionKind.ATTEMPT:
            assert proposal.execute_at <= calendar.pdn_deadline_for(
                proposal.execute_at
            ) + dt.timedelta(hours=calendar.pdn_lead_time_hours)


def test_holds_when_the_case_is_closed(calendar, classifier, config):
    plan = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(outcome=CaseOutcome.RECOVERED), NOW
    )
    assert plan.action is DecisionAction.HOLD
    assert not plan.proposals


# --------------------------------------------------------------------------- #
# OPEN QUESTION 1: rail
# --------------------------------------------------------------------------- #


def test_default_assumption_is_rail_agnostic_cells(calendar, classifier, config):
    """Documented assumption: the rail does not change which cell fires."""
    plans = {
        rail: arm_for(
            calendar, classifier, config, ("TERMINAL", "HIGH")
        ).plan(make_case_view(rail=rail), NOW)
        for rail in ("card", "upi", "emandate")
    }
    assert len({plan.action for plan in plans.values()}) == 1


def test_emandate_holds_while_a_prior_execution_is_unresolved(
    calendar, classifier, config
):
    """Asynchronous rail: holding is the only legal move, not indecision."""
    plan = arm_for(calendar, classifier, config, ("INFRASTRUCTURE", "HIGH")).plan(
        make_case_view(rail="emandate", attempt_pending=True), NOW
    )
    assert plan.action is DecisionAction.HOLD
    assert "unresolved" in plan.reason


def test_migration_targets_validate_against_the_graph(calendar, classifier, config):
    arm = ArmC(calendar, classifier, config)
    assert arm.migration_targets(make_case_view(rail="upi")) == frozenset({"card"})
    assert arm.migration_targets(make_case_view(rail="emandate")) == frozenset({"card"})
    assert arm.migration_targets(make_case_view(rail="card")) == MIGRATION_GRAPH["card"]


def test_rail_policy_is_pluggable(calendar, classifier, config):
    """The alternative answer to open question 1, swapped in without edits."""
    arm = arm_for(
        calendar,
        classifier,
        config,
        ("TERMINAL", "HIGH"),
        rail_policy=EmandateHoldsTerminal(),
    )
    emandate = arm.plan(make_case_view(rail="emandate"), NOW)
    upi = arm.plan(make_case_view(rail="upi"), NOW)

    assert emandate.action is DecisionAction.SURRENDER
    assert upi.action is DecisionAction.OFFER_RAIL_MIGRATION


# --------------------------------------------------------------------------- #
# OPEN QUESTION 2: what the cap counts (CHALLENGES 008)
# --------------------------------------------------------------------------- #


def test_default_counter_treats_attempts_used_as_executions(calendar, classifier, config):
    view = make_case_view(attempts_used=3, contacts_used=2)
    assert SystemInitiatedOnly().executions_used(view) == 3


def test_alternative_counter_discounts_customer_attempts(calendar, classifier, config):
    """Models the production case where order.attempts conflates the two."""
    view = make_case_view(attempts_used=3, contacts_used=2)
    assert DiscountCustomerAttempts().executions_used(view) == 1


def test_counter_never_discounts_below_the_original_execution():
    view = make_case_view(attempts_used=1, contacts_used=5)
    assert DiscountCustomerAttempts().executions_used(view) == 1


def test_counter_choice_changes_when_the_arm_surrenders(calendar, classifier, config):
    """The open question is load-bearing: the two answers behave differently."""
    view = make_case_view(attempts_used=4, contacts_used=2)

    strict = arm_for(
        calendar, classifier, config, ("INFRASTRUCTURE", "HIGH"),
        execution_counter=SystemInitiatedOnly(),
    ).plan(view, NOW)
    lenient = arm_for(
        calendar, classifier, config, ("INFRASTRUCTURE", "HIGH"),
        execution_counter=DiscountCustomerAttempts(),
    ).plan(view, NOW)

    assert strict.action is DecisionAction.SURRENDER
    assert lenient.action is DecisionAction.SCHEDULE_AT


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def test_plugs_into_run_comparison(calendar, classifier, config, small_world):
    result = run_comparison(
        [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)],
        small_world,
        calendar,
        costs=classifier.config.costs,
    )
    assert set(result.metrics) == {"A", "B", "C"}
    assert result.metrics["C"].cases == result.metrics["A"].cases


def test_spends_fewer_attempts_on_unrecoverable_failures_than_the_baseline(
    calendar, classifier, config, small_world
):
    """The primary claim, measured. Definitional, not a tuning result."""
    result = run_comparison(
        [ArmA(calendar), ArmC(calendar, classifier, config)],
        small_world,
        calendar,
        costs=classifier.config.costs,
    )
    assert (
        result.metrics["C"].terminal_attempts_wasted
        < result.metrics["A"].terminal_attempts_wasted
    )
