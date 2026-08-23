"""Horizon sensitivity and classifier-coverage diagnostics.

Both exist to make something visible that would otherwise be argued about: at
what lifetime preservation outweighs cycle recovery, and which keys the
taxonomy does not cover.
"""

from __future__ import annotations

import pathlib

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


def test_purity_is_computed_from_the_ground_truth_mix():
    from recovery.coverage import KeyStats

    stats = KeyStats(key=("upi", "bank", "step", "reason"), rail="upi", count=10)
    stats.true_classes.update(["TERMINAL"] * 7 + ["LIQUIDITY"] * 3)
    assert stats.purity == pytest.approx(0.7)
    assert stats.dominant_true_class == "TERMINAL"


def test_remaining_unmapped_keys_are_the_unambiguous_ones(classifier):
    """The ambiguous keys were given deliberately-low rows, so what is left
    uncovered should be high-purity -- keys that deserve a confident row."""
    report = analyse(classifier, seeds=(11, 42, 101))
    for stats in report.unmapped_keys:
        assert stats.purity >= 0.9, (
            f"{stats.describe} is ambiguous ({stats.purity:.0%}) and still has no row; "
            "an ambiguous key should get a deliberately-low row, not be left out"
        )


# ------------------------------------------------- exit-door exchange ------ #


def test_exchange_rate_is_reported_as_a_band(arms, small_world, calendar, classifier):
    from recovery.sim.run import exchange_rate_band

    band = exchange_rate_band(
        arms,
        small_world,
        calendar,
        mandate_hazard_range(load_world_config()),
        "C",
        "A",
        points=3,
        costs=classifier.config.costs,
    )
    assert len(band.rates) == 3
    assert "across the swept hazard range" in band.describe() or not band.values


def test_exchange_rate_is_halts_per_revocation_avoided():
    from recovery.sim.run import ExchangeRate

    rate = ExchangeRate("C", "A", 0.02, revocations_avoided=20, halts_added=30)
    assert rate.rate == pytest.approx(1.5)


def test_exchange_rate_undefined_when_no_revocations_avoided():
    """Not zero, not infinity -- the question does not arise."""
    from recovery.sim.run import ExchangeRate

    assert ExchangeRate("C", "A", 0.02, 0, 30).rate is None


def test_allocator_trades_revocations_for_halts(arms, small_world, calendar, classifier):
    """The trade the exchange rate exists to make visible."""
    from recovery.sim.run import run_comparison

    result = run_comparison(arms, small_world, calendar, costs=classifier.config.costs)
    assert result.metrics["C"].mandates_revoked < result.metrics["A"].mandates_revoked
    assert result.metrics["C"].mandates_halted > result.metrics["A"].mandates_halted


# ---------------------------------------- deliberately low confidence ------ #


def test_deliberate_low_rows_are_distinguishable_from_unmapped(classifier):
    """"We looked and cannot tell" is a different state from "we never looked"."""
    from recovery.fixtures import CAPTURED_GENERIC_DECLINE
    from recovery.normalize import normalize_entity

    deliberate = classifier.classify(
        normalize_entity(
            {
                "method": "upi",
                "error_source": "beneficiary_bank",
                "error_step": "payment_debit_response",
                "error_reason": "mandate_revoked",
            },
            source_space=classifier.config.source_space,
        )
    )
    unmapped = classifier.classify(
        normalize_entity(
            {"method": "upi", "error_step": "nothing_maps_this"},
            source_space=classifier.config.source_space,
        )
    )

    assert deliberate.mapped is True
    assert deliberate.deliberately_low_confidence is True
    assert deliberate.rule_index is not None
    assert unmapped.mapped is False
    assert unmapped.deliberately_low_confidence is False

    # Both still land in the LOW row -- same action, different record.
    assert deliberate.band.value == "LOW"
    assert unmapped.band.value == "LOW"


def test_deliberate_low_rows_preserve_the_intended_class(classifier):
    """The dominant reading survives in cost_resolved_from, for the audit trail."""
    from recovery.models import FailureClass
    from recovery.normalize import normalize_entity

    result = classifier.classify(
        normalize_entity(
            {
                "method": "card",
                "error_source": "gateway",
                "error_step": "payment_initiation",
                "error_reason": "gateway_technical_error",
            },
            source_space=classifier.config.source_space,
        )
    )
    assert result.cost_resolved_from is FailureClass.INFRASTRUCTURE
    assert result.band.value == "LOW"


def test_contradictory_deliberate_low_is_rejected(tmp_path):
    """A rule cannot claim deliberate low confidence while banding above LOW."""
    import yaml
    from recovery.classifier import ClassifierConfigError, load_classifier

    data = yaml.safe_load(pathlib.Path("config/classifier.yaml").read_text(encoding="utf-8"))
    data["rules"] = [
        {
            "match": {"method": "upi"},
            "class": "LIQUIDITY",
            "confidence": 0.95,
            "deliberately_low_confidence": True,
        }
    ]
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ClassifierConfigError, match="deliberately_low_confidence"):
        load_classifier(path, allow_stub=True)


def test_only_the_two_high_purity_keys_remain_unmapped(classifier):
    """The six ambiguous keys now have rows; the confident two are left to author."""
    report = analyse(classifier, seeds=(11, 42, 101, 202, 303))
    remaining = {stats.describe for stats in report.unmapped_keys}
    assert all("insufficient_funds" in key for key in remaining), remaining
