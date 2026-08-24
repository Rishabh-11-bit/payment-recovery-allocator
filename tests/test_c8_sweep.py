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

import pathlib

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
    # The failure mix now comes from a calibration profile, not inline
    # constants; stress must not silently swap the profile either.
    assert stressed["batch"]["calibration_profile"] == (
        nominal["batch"]["calibration_profile"]
    )
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


# ----------------------------------------------------- calibration (C9) --- #


def test_world_records_which_profile_its_mix_came_from():
    """A reported mix must be traceable to its provenance, or its absence."""
    assert sample_world(seed=3).calibration_profile == "uncalibrated"


def test_inline_class_mix_is_reported_as_an_override_not_a_profile():
    """A result taken from an override has no provenance and must not borrow one."""
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "class_mix": {"INFRASTRUCTURE": [1, 1],
                                                  "LIQUIDITY": [1, 1],
                                                  "ATTENTION": [1, 1],
                                                  "TERMINAL": [1, 1]}}
    assert sample_world(seed=3, raw=raw).calibration_profile == "inline-override"


def test_a_batch_with_neither_profile_nor_mix_is_rejected():
    from recovery.sim.world import WorldConfigError

    raw = load_world_config()
    raw["batch"] = {k: v for k, v in raw["batch"].items() if k != "calibration_profile"}
    with pytest.raises(WorldConfigError, match="calibration_profile"):
        sample_world(seed=1, raw=raw)


def test_bounded_profile_sweeps_the_full_simplex():
    """The split is unavailable in published data, so it must not be pinned."""
    import random

    from recovery.calibration import load_profile

    profile = load_profile("bounded-2026")
    rng = random.Random(1)
    mixes = [profile.sample_mix(rng) for _ in range(2000)]

    for name in ("LIQUIDITY", "INFRASTRUCTURE", "TERMINAL"):
        values = [m[name] for m in mixes]
        assert min(values) < 0.10, f"{name} never gets a small share"
        assert max(values) > 0.60, f"{name} never gets a large share"

    # No preference between the three: Razorpay lists them in an order, and that
    # order is weak ordinal evidence at most -- never a proportion.
    medians = [
        sorted(m[name] for m in mixes)[len(mixes) // 2]
        for name in ("LIQUIDITY", "INFRASTRUCTURE", "TERMINAL")
    ]
    assert max(medians) - min(medians) < 0.03, medians


def test_bounded_profile_mix_is_a_distribution():
    import random

    from recovery.calibration import load_profile

    rng = random.Random(2)
    for mix in (load_profile("bounded-2026").sample_mix(rng) for _ in range(200)):
        assert abs(sum(mix.values()) - 1.0) < 1e-9
        assert set(mix) == {"LIQUIDITY", "INFRASTRUCTURE", "TERMINAL", "ATTENTION"}


def test_attention_is_bounded_because_no_source_speaks_to_it():
    import random

    from recovery.calibration import load_profile

    profile = load_profile("bounded-2026")
    low, high = profile.attention_share
    rng = random.Random(3)
    values = [profile.sample_mix(rng)["ATTENTION"] for _ in range(500)]
    assert low <= min(values) and max(values) <= high


def test_a_calibrated_profile_must_record_its_interpretation(tmp_path):
    """Citing sources without saying what was inferred is the failure mode."""
    import yaml

    from recovery.calibration import CalibrationError, load_profile

    data = yaml.safe_load(
        pathlib.Path("config/calibration/bounded-2026.yaml").read_text(encoding="utf-8")
    )
    data.pop("interpretation")
    (tmp_path / "broken.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CalibrationError, match="interpretation"):
        load_profile("broken", directory=tmp_path)


def test_uncalibrated_profile_needs_no_interpretation():
    """An honest invention is labelled and needs no justification."""
    from recovery.calibration import load_profile

    profile = load_profile("uncalibrated")
    assert not profile.is_calibrated
    assert profile.derives_from == ()


def test_bounded_profile_carries_sourced_outage_figures():
    from recovery.calibration import load_profile, read_downtime, summarise_downtime

    profile = load_profile("bounded-2026")
    outage = profile.issuer_outage
    summary = summarise_downtime(read_downtime())

    # Recomputable from data/, not transcribed. If the files change, these must.
    assert outage["distinct_banks"] == summary.distinct_banks_normalised
    assert outage["persistent"]["count"] == len(summary.appear_every_month)
    assert outage["months_observed"] == len(summary.months)


def test_world_carries_the_sourced_outage_through():
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "calibration_profile": "bounded-2026"}
    world = sample_world(seed=1, raw=raw)
    assert world.issuer_outage["persistent"]["count"] == 2
