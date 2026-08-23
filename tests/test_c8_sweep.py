"""C8 -- robustness sweep, breaking points, crossover distribution.

These test the sweep machinery rather than the sweep's findings. What the
findings *are* is a property of the world ranges and the arms, and asserting a
particular breaking point here would pin the result to today's parameters and
turn a robustness tool into a regression test for one answer.

What is asserted: the sweep samples the space it claims to, the stress ranges
really are wider, the stump finds a planted condition, and a distribution with
no crossover reports that rather than hiding it.
"""

from __future__ import annotations

import pytest

from allocator.arm_c import ArmC
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import calendar_from_config
from recovery.sim.sweep import (
    MIN_BUCKET,
    BreakingPoint,
    CrossoverDistribution,
    SweepReport,
    WorldOutcome,
    breaking_points,
    crossover_distribution,
    stress_config,
    sweep,
    world_parameters,
)
from recovery.sim.world import load_world_config, sample_world


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def arms(calendar, classifier, config):
    return [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)]


@pytest.fixture
def small_raw():
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 40}
    return raw


@pytest.fixture
def outcomes(arms, calendar, small_raw, classifier):
    return sweep(arms, calendar, small_raw, worlds=25, costs=classifier.config.costs)


# --------------------------------------------------------------- sampling -- #


def test_sweep_visits_a_distinct_world_per_seed(outcomes):
    assert len({o.seed for o in outcomes}) == len(outcomes)
    fidelities = {round(o.parameters["emission_fidelity"], 6) for o in outcomes}
    assert len(fidelities) > 1, "every world drew the same parameters"


def test_sweep_is_deterministic(arms, calendar, small_raw, classifier):
    first = sweep(arms, calendar, small_raw, worlds=5, costs=classifier.config.costs)
    second = sweep(arms, calendar, small_raw, worlds=5, costs=classifier.config.costs)
    assert [o.cycle_paise for o in first] == [o.cycle_paise for o in second]
    assert [o.crossover for o in first] == [o.crossover for o in second]


def test_every_arm_runs_in_every_world(outcomes):
    for outcome in outcomes:
        assert set(outcome.cycle_paise) == {"A", "B", "C"}
        assert set(outcome.preserved) == {"A", "B", "C"}


def test_world_parameters_cover_the_swept_dimensions():
    params = world_parameters(sample_world(seed=3))
    for expected in (
        "emission_fidelity",
        "revocation_per_notification",
        "recovery_LIQUIDITY_per_day",
        "recovery_INFRASTRUCTURE_base",
        "link_conversion_TERMINAL",
        "class_mix_TERMINAL",
        "rail_mix_upi",
    ):
        assert expected in params, expected


def test_terminal_recovery_is_not_a_swept_parameter():
    """P(retry succeeds | TERMINAL) = 0 is definitional and must not be swept."""
    params = world_parameters(sample_world(seed=3))
    assert not [key for key in params if key.startswith("recovery_TERMINAL")]


# ------------------------------------------------------------ stress ------ #


def test_stress_widens_the_ranges_it_names():
    nominal = load_world_config()
    stressed = stress_config(nominal)

    assert nominal["recovery"]["LIQUIDITY"]["per_day"][0] > 0.0
    assert stressed["recovery"]["LIQUIDITY"]["per_day"][0] == 0.0, (
        "stress must reach time-independent liquidity -- the assumption's own edge"
    )
    assert (
        stressed["link_conversion"]["TERMINAL"][1]
        > nominal["link_conversion"]["TERMINAL"][1]
    )
    assert stressed["emission"]["fidelity"][0] < nominal["emission"]["fidelity"][0]


def test_stress_leaves_untouched_ranges_alone():
    nominal = load_world_config()
    stressed = stress_config(nominal)
    assert stressed["batch"]["class_mix"] == nominal["batch"]["class_mix"]
    assert stressed["recovery"]["TERMINAL"] == nominal["recovery"]["TERMINAL"]


def test_stress_block_is_removed_from_the_merged_config():
    """Otherwise `sample_world` would trip over an unknown top-level key."""
    assert "stress" not in stress_config(load_world_config())


def test_stress_config_does_not_mutate_its_input():
    nominal = load_world_config()
    before = nominal["recovery"]["LIQUIDITY"]["per_day"][0]
    stress_config(nominal)
    assert nominal["recovery"]["LIQUIDITY"]["per_day"][0] == before


# --------------------------------------------------- breaking points ------ #


def make_outcome(seed: int, value: float, *, wins: bool) -> WorldOutcome:
    return WorldOutcome(
        seed=seed,
        parameters={"probe": value, "noise": (seed * 37 % 100) / 100},
        cycle_paise={"C": 100, "A": 100},
        preserved={"C": 10, "A": 5},
        monthly_value_paise=1000.0,
        crossover={"A": 1.0 if wins else 400.0},
        already_ahead={"A": False},
    )


def test_stump_finds_a_planted_condition():
    """C loses exactly when `probe` is high; the sweep must say so."""
    outcomes = [make_outcome(i, 0.1, wins=True) for i in range(40)]
    outcomes += [make_outcome(100 + i, 0.9, wins=False) for i in range(40)]

    points = breaking_points(outcomes, "A")
    assert points
    assert points[0].parameter == "probe"
    assert points[0].direction == "above"
    assert points[0].loss_rate_inside > points[0].loss_rate_outside


def test_stump_reports_nothing_when_nothing_separates():
    """No condition is better than an invented one."""
    outcomes = [make_outcome(i, (i % 10) / 10, wins=True) for i in range(60)]
    assert breaking_points(outcomes, "A") == []


def test_stump_ignores_splits_that_are_too_small():
    """A 'condition' resting on three worlds is an outlier with a decimal point."""
    outcomes = [make_outcome(i, 0.1, wins=True) for i in range(60)]
    outcomes += [make_outcome(900 + i, 0.99, wins=False) for i in range(MIN_BUCKET - 5)]
    for point in breaking_points(outcomes, "A"):
        assert point.worlds_inside >= MIN_BUCKET
        assert point.worlds_outside >= MIN_BUCKET


def test_breaking_point_describes_both_sides():
    """A loss rate without its complement is not a condition."""
    point = BreakingPoint(
        parameter="link_conversion_TERMINAL",
        threshold=0.14,
        direction="above",
        loss_rate_inside=0.71,
        loss_rate_outside=0.04,
        worlds_inside=60,
        worlds_outside=240,
    )
    described = point.describe()
    assert "71%" in described and "4%" in described
    assert "60" in described and "240" in described


# ------------------------------------------------ crossover distribution -- #


def test_distribution_separates_never_from_already_ahead():
    outcomes = [
        WorldOutcome(1, {}, {}, {}, 1.0, {"B": None}, {"B": False}),
        WorldOutcome(2, {}, {}, {}, 1.0, {"B": 0.0}, {"B": True}),
        WorldOutcome(3, {}, {}, {}, 1.0, {"B": 4.0}, {"B": False}),
    ]
    distribution = crossover_distribution(outcomes, "B")
    assert distribution.never == 1
    assert distribution.already_ahead == 1
    assert distribution.months == (4.0,)


def test_distribution_reports_never_overtaking(outcomes):
    """Where the ordering does not invert, that is the result."""
    empty = CrossoverDistribution(
        incumbent="B", months=(), never=50, already_ahead=0, total=50
    )
    assert "never overtakes" in empty.describe()


def test_distribution_is_a_spread_not_a_point(outcomes):
    distribution = crossover_distribution(outcomes, "A")
    if len(distribution.months) > 2:
        assert distribution.percentile(0.1) <= distribution.percentile(0.5)
        assert distribution.percentile(0.5) <= distribution.percentile(0.9)


def test_win_rate_rises_with_the_horizon(outcomes):
    distribution = crossover_distribution(outcomes, "B")
    assert distribution.win_rate(6) <= distribution.win_rate(12) <= distribution.win_rate(24)


def test_report_exposes_cycle_winner_share(outcomes):
    report = SweepReport(label="test", worlds=len(outcomes), outcomes=outcomes)
    shares = [report.cycle_win_share(name) for name in ("A", "B", "C")]
    assert sum(shares) == pytest.approx(1.0)


def test_sweep_records_a_crossover_for_every_incumbent(outcomes):
    for outcome in outcomes:
        assert set(outcome.crossover) == {"A", "B"}
        assert set(outcome.already_ahead) == {"A", "B"}
