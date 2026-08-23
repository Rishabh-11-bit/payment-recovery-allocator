"""Horizon sensitivity and classifier-coverage diagnostics.

Both exist to make something visible that would otherwise be argued about: at
what lifetime preservation outweighs cycle recovery, and which keys the
taxonomy does not cover.
"""

from __future__ import annotations

import pytest

from allocator.arm_c import ArmC
from recovery.coverage import analyse
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import calendar_from_config
from recovery.sim.horizon import (
    CrossoverBand,
    Crossover,
    SurvivalBasis,
    horizon_sweep,
)
from recovery.sim.world import load_world_config, mandate_hazard_range, sample_world


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def small_world():
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 120}
    return sample_world(seed=7, raw=raw)


@pytest.fixture
def arms(calendar, classifier, config):
    return [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)]


@pytest.fixture
def sweep(arms, small_world, calendar, classifier):
    return horizon_sweep(
        arms,
        small_world,
        calendar,
        mandate_hazard_range(load_world_config()),
        hazard_points=3,
        costs=classifier.config.costs,
    )


# --------------------------------------------------------------- horizon --- #


def test_sweep_covers_the_whole_hazard_range(sweep):
    low, high = mandate_hazard_range(load_world_config())
    assert sweep.hazard_points[0] == pytest.approx(low)
    assert sweep.hazard_points[-1] == pytest.approx(high)


def test_monthly_value_is_derived_from_the_batch_not_assumed(sweep, small_world):
    """It is the mean subscription charge, not a new parameter."""
    low, high = small_world.amount_paise
    assert low <= sweep.monthly_value_paise <= high


def test_value_is_linear_in_lifetime(sweep):
    """The annuity term scales; the cycle term does not."""
    one = sweep.value_paise("A", 0, 1)
    two = sweep.value_paise("A", 0, 2)
    three = sweep.value_paise("A", 0, 3)
    assert (two - one) == pytest.approx(three - two)


def test_analytic_crossover_matches_a_brute_force_scan(sweep):
    """The closed form must agree with actually walking the horizons."""
    band = sweep.crossover("C", "B")
    for index, crossover in enumerate(band.crossovers):
        if crossover.months is None or crossover.already_ahead:
            continue
        scanned = next(
            months
            for months in range(0, 400)
            if sweep.value_paise("C", index, months) >= sweep.value_paise("B", index, months)
        )
        assert abs(scanned - crossover.months) <= 1


def test_crossover_is_reported_as_a_band_not_a_point(sweep):
    band = sweep.crossover("C", "B")
    assert len(band.crossovers) == len(sweep.hazard_points)
    assert "across the swept hazard range" in band.describe() or band.never_count


def test_value_band_spans_the_hazard_range(sweep):
    low, high = sweep.value_band_inr("C", 12)
    assert low <= high


def test_never_overtaking_is_reported_not_hidden():
    """An arm that recovers less and preserves no more never catches up."""
    band = CrossoverBand(
        challenger="C",
        incumbent="B",
        basis=SurvivalBasis.NOT_REVOKED,
        crossovers=(
            Crossover("C", "B", 0.01, months=None, already_ahead=False),
            Crossover("C", "B", 0.04, months=None, already_ahead=False),
        ),
    )
    assert band.never_count == 2
    assert "never overtakes" in band.describe()


def test_already_ahead_is_distinguished_from_never():
    band = CrossoverBand(
        challenger="C",
        incumbent="A",
        basis=SurvivalBasis.NOT_REVOKED,
        crossovers=(Crossover("C", "A", 0.01, months=0.0, already_ahead=True),),
    )
    assert band.always_count == 1
    assert "at any horizon" in band.describe()


def test_working_basis_is_degenerate_and_says_so(arms, small_world, calendar, classifier):
    """preserved - halted == cases_recovered, so the basis restates the cycle term.

    Kept and labelled rather than dropped: a reader who sees only the
    not_revoked basis cannot tell whether the other was tried.
    """
    assert SurvivalBasis.WORKING.is_degenerate
    assert not SurvivalBasis.NOT_REVOKED.is_degenerate

    working = horizon_sweep(
        arms,
        small_world,
        calendar,
        mandate_hazard_range(load_world_config()),
        hazard_points=2,
        basis=SurvivalBasis.WORKING,
        costs=classifier.config.costs,
    )
    from recovery.sim.run import run_comparison

    result = run_comparison(
        arms,
        small_world.with_mandate_hazard(working.hazard_points[0]),
        calendar,
        costs=classifier.config.costs,
    )
    for name in ("A", "B", "C"):
        assert working.preserved[name][0] == result.metrics[name].cases_recovered


def test_no_single_hazard_is_reported_alone(sweep):
    """Discipline check: the describe string never quotes one hazard's answer."""
    band = sweep.crossover("C", "B")
    if band.never_count == len(band.crossovers) or band.always_count == len(band.crossovers):
        return
    assert "hazard range" in band.describe()


# -------------------------------------------------------------- coverage --- #


def test_coverage_report_accounts_for_every_failure(classifier):
    report = analyse(classifier, seeds=(11, 42))
    assert report.total > 0
    assert report.mapped + report.unmapped == report.total
    assert sum(stats.count for stats in report.unmapped_keys) == report.unmapped


def test_unmapped_keys_are_ranked_by_frequency(classifier):
    report = analyse(classifier, seeds=(11, 42))
    counts = [stats.count for stats in report.unmapped_keys]
    assert counts == sorted(counts, reverse=True)


def test_unmapped_keys_carry_ground_truth(classifier):
    report = analyse(classifier, seeds=(11, 42))
    assert report.unmapped_keys, "the stub taxonomy must leave keys uncovered"
    for stats in report.unmapped_keys:
        assert stats.true_classes
        assert sum(stats.true_classes.values()) == stats.count


def test_unmapped_keys_are_full_four_part_tuples(classifier):
    report = analyse(classifier, seeds=(11,))
    for stats in report.unmapped_keys:
        assert len(stats.key) == 4
        assert stats.describe.count("/") == 3


def test_pooling_seeds_finds_more_than_one(classifier):
    """A taxonomy written against one batch inherits that batch's accidents."""
    one = analyse(classifier, seeds=(11,))
    many = analyse(classifier, seeds=(11, 42, 101, 202, 303))
    assert many.total > one.total
    assert len(many.unmapped_keys) >= len(one.unmapped_keys)


def test_purity_flags_keys_no_single_row_can_serve(classifier):
    report = analyse(classifier, seeds=(11, 42, 101))
    mixed = [s for s in report.unmapped_keys if s.purity < 0.9]
    assert mixed, "noisy emission must produce keys with mixed ground truth"
    for stats in mixed:
        assert 0.0 < stats.purity < 1.0
