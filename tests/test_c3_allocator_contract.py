"""The contract Arm C has to satisfy.

Written before the allocator, deliberately. These tests define what "correct"
means for C3, so the policy is written against a fixed target rather than the
target being adjusted afterwards to match whatever got written.

While `ArmC.propose` raises `NotImplementedError`, the tests that need a working
allocator skip with a clear reason and the rest run. Nothing here asserts what
the allocator should *decide* -- that is policy, and it is hand-authored. What
is asserted is that whatever it decides stays inside the budget, covers every
case it can be handed, and reasons about orderings rather than magnitudes.
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from allocator.arm_c import ArmC
from recovery.contract import (
    FixedClassifier,
    allocator_modules,
    assert_within_execution_budget,
    classification_grid,
    confidence_for_band,
    drive,
    edge_classifications,
    execution_budget_violations,
    make_case_view,
    ordinal_violations,
    remaining_execution_budget,
)
from recovery.models import ConfidenceBand, FailureClass
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import IST, calendar_from_config
from recovery.sim.environment import ActionKind, Proposal
from recovery.sim.world import load_world_config, sample_world


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def small_world():
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 40}
    return sample_world(seed=7, raw=raw)


@pytest.fixture
def arm_c(calendar, classifier):
    return ArmC(calendar, classifier)


def implemented(arm) -> bool:
    try:
        arm.propose(make_case_view(), dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST))
    except NotImplementedError:
        return False
    except Exception:
        # Any other error means it is written and broken, which the tests below
        # should surface rather than skip past.
        return True
    return True


def require_allocator(arm) -> None:
    if not implemented(arm):
        pytest.skip("ArmC.propose is not implemented yet (C3 is hand-authored)")


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_arm_c_has_the_arm_interface(arm_c):
    """It must plug into run_comparison without adaptation."""
    assert arm_c.name == "C"
    signature = inspect.signature(arm_c.propose)
    assert list(signature.parameters) == ["view", "now"]


def test_arm_c_classify_is_wired(arm_c):
    """The classification path works even though the policy does not."""
    result = arm_c.classify(make_case_view())
    assert result.failure_class in set(FailureClass)
    assert result.band in set(ConfidenceBand)


def test_unimplemented_arm_says_so_clearly(calendar, classifier):
    arm = ArmC(calendar, classifier)
    if implemented(arm):
        pytest.skip("already implemented")
    with pytest.raises(NotImplementedError, match="hand-authored"):
        arm.propose(make_case_view(), dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST))


# --------------------------------------------------------------------------- #
# The twelve combinations
# --------------------------------------------------------------------------- #


def test_grid_covers_every_class_and_band(classifier):
    grid = classification_grid(classifier)
    assert len(grid) == 12
    assert {key[0] for key in grid} == {c.value for c in FailureClass}
    assert {key[1] for key in grid} == {b.value for b in ConfidenceBand}


def test_grid_confidences_land_in_their_band(classifier):
    for (_, band_name), classification in classification_grid(classifier).items():
        assert classification.band.value == band_name
        assert classifier.band_for(classification.confidence).value == band_name


def test_only_high_band_fixtures_permit_exclusion(classifier):
    for (_, band_name), classification in classification_grid(classifier).items():
        assert classification.may_exclude_instrument is (band_name == "HIGH")


def test_band_confidences_are_derived_from_config_not_hardcoded(classifier):
    """The grid must follow the config, or it will drift from it silently."""
    assert confidence_for_band(classifier, ConfidenceBand.HIGH) >= (
        classifier.config.high_threshold
    )
    assert confidence_for_band(classifier, ConfidenceBand.LOW) < (
        classifier.config.moderate_threshold
    )


@pytest.mark.parametrize(
    "failure_class", [c for c in FailureClass], ids=lambda c: c.value
)
@pytest.mark.parametrize("band", [b for b in ConfidenceBand], ids=lambda b: b.value)
def test_allocator_answers_every_combination(arm_c, classifier, failure_class, band):
    """Twelve cases, twelve answers. No combination may raise or hang."""
    require_allocator(arm_c)
    grid = classification_grid(classifier)
    fixed = FixedClassifier(
        grid[(failure_class.value, band.value)], classifier.config
    )
    arm = ArmC(arm_c.calendar, fixed)

    proposals = arm.propose(
        make_case_view(), dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
    )
    assert isinstance(proposals, list)
    assert all(isinstance(p, Proposal) for p in proposals)


def test_terminal_is_never_retried_at_high_confidence(arm_c, classifier):
    """Definitional: P(retry succeeds | TERMINAL) = 0. An execution here is waste.

    This is the one behavioural assertion in the file, and it holds by
    definition rather than by policy preference. A HIGH-confidence TERMINAL that
    still proposes a mandate execution is spending a capped resource on a
    recovery that cannot happen.
    """
    require_allocator(arm_c)
    grid = classification_grid(classifier)
    fixed = FixedClassifier(grid[("TERMINAL", "HIGH")], classifier.config)
    arm = ArmC(arm_c.calendar, fixed)

    proposals = arm.propose(
        make_case_view(), dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
    )
    assert not [p for p in proposals if p.kind is ActionKind.ATTEMPT]


@pytest.mark.parametrize(
    "name", ["unmapped", "terminal_instrument", "terminal_merchant_configuration",
             "generic_decline", "source_undocumented"]
)
def test_allocator_answers_every_captured_edge_case(arm_c, classifier, name):
    """Each of these corresponds to a real payload in tests/fixtures/payments."""
    require_allocator(arm_c)
    fixed = FixedClassifier(edge_classifications(classifier)[name], classifier.config)
    arm = ArmC(arm_c.calendar, fixed)

    proposals = arm.propose(
        make_case_view(), dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
    )
    assert isinstance(proposals, list)


# --------------------------------------------------------------------------- #
# Mandate-execution budget
# --------------------------------------------------------------------------- #


def test_remaining_budget_excludes_the_original_execution(calendar):
    """The original debit already failed. That is what opened the case."""
    assert remaining_execution_budget(calendar) == calendar.attempt_cap - 1


def test_harness_passes_the_documented_baseline(small_world, calendar):
    """Arm A spends exactly T+1/T+2/T+3. If the harness fails it, the harness is wrong."""
    assert_within_execution_budget(ArmA(calendar), small_world, calendar)


def test_harness_ignores_contacts(small_world, calendar):
    """Arm B contacts every case and must still pass: a contact is not an execution.

    This is CHALLENGES 008 encoded as a test. Counting link attempts against the
    NPCI budget would surrender mandates that still have executions left.
    """
    assert_within_execution_budget(ArmB(calendar), small_world, calendar)

    contacts = [
        proposal
        for _, proposal in drive(ArmB(calendar), small_world, calendar)
        if proposal.kind is ActionKind.CONTACT
    ]
    assert contacts, "Arm B must contact, or this proves nothing"


def test_harness_catches_an_overspending_arm(small_world, calendar):
    """A harness that never fails is not a harness."""

    class Greedy:
        name = "greedy"

        def propose(self, view, now):
            return [
                Proposal(
                    case_id=view.case_id,
                    kind=ActionKind.ATTEMPT,
                    execute_at=now + dt.timedelta(days=2),
                )
            ]

    violations = execution_budget_violations(Greedy(), small_world, calendar)
    assert violations
    assert all(v.executions_proposed > v.budget for v in violations)
    with pytest.raises(AssertionError, match="exceeded the mandate-execution budget"):
        assert_within_execution_budget(Greedy(), small_world, calendar)


def test_harness_measures_what_was_asked_for_not_what_was_allowed(small_world, calendar):
    """An arm cannot look compliant because the environment rejected it.

    `drive` never executes, so a peak-hour proposal still counts against the
    budget. Compliance and budget are separate failures and must stay separable.
    """

    class PeakHourGreedy:
        name = "peak_greedy"

        def propose(self, view, now):
            return [
                Proposal(
                    case_id=view.case_id,
                    kind=ActionKind.ATTEMPT,
                    execute_at=(now + dt.timedelta(days=2)).replace(hour=11),
                )
            ]

    assert execution_budget_violations(PeakHourGreedy(), small_world, calendar)


def test_arm_c_respects_the_execution_budget(arm_c, small_world, calendar):
    require_allocator(arm_c)
    assert_within_execution_budget(arm_c, small_world, calendar)


def test_drive_calls_propose_once_per_case_per_day(small_world, calendar):
    """A stateful allocator must not be polled twice for the same decision."""
    calls: list[tuple[str, dt.datetime]] = []

    class Counting:
        name = "counting"

        def propose(self, view, now):
            calls.append((view.case_id, now))
            return []

    list(drive(Counting(), small_world, calendar, days=3))
    assert len(calls) == len(set(calls))


# --------------------------------------------------------------------------- #
# Ordinal-only
# --------------------------------------------------------------------------- #


def test_allocator_reads_orderings_not_probabilities():
    """Policy may depend on ordinal facts, never on cardinal ones.

    Static, because the failure is a shape of reasoning rather than an event: an
    allocator that hardcodes 0.41 never raises, it just encodes a number nobody
    can defend. There is nothing to catch at runtime.
    """
    modules = allocator_modules()
    assert modules, "no allocator modules found -- expected allocator/*.py"

    violations = ordinal_violations(modules)
    assert not violations, "cardinal reasoning in the policy path:\n  " + "\n  ".join(
        str(violation) for violation in violations
    )


def test_ordinal_check_catches_a_probability_literal(tmp_path):
    source = tmp_path / "bad.py"
    source.write_text(
        "def decide(c):\n    return c.confidence > 0.41\n", encoding="utf-8"
    )
    kinds = {v.kind for v in ordinal_violations([source])}
    assert "probability-literal" in kinds
    assert "confidence-threshold" in kinds


def test_ordinal_check_allows_integers(tmp_path):
    """Attempt counts and day offsets are ordinal and must not be flagged."""
    source = tmp_path / "fine.py"
    source.write_text(
        "def decide(view):\n"
        "    if view.attempts_used >= 4:\n"
        "        return []\n"
        "    return [1, 2, 3]\n",
        encoding="utf-8",
    )
    assert ordinal_violations([source]) == []


def test_ordinal_check_catches_ground_truth_access(tmp_path):
    source = tmp_path / "peeking.py"
    source.write_text("def decide(f):\n    return f.true_class\n", encoding="utf-8")
    assert {v.kind for v in ordinal_violations([source])} == {"ground-truth-access"}


def test_ordinal_check_catches_a_forbidden_import(tmp_path):
    source = tmp_path / "importer.py"
    source.write_text("from recovery.sim.world import World\n", encoding="utf-8")
    assert {v.kind for v in ordinal_violations([source])} == {"forbidden-import"}


def test_ordinal_check_suppression_is_explicit(tmp_path):
    """An escape hatch that is visible and greppable, not a silent exception."""
    source = tmp_path / "suppressed.py"
    source.write_text(
        "JITTER = 0.25  # ordinal-ok: storm governor jitter, not a recovery probability\n",
        encoding="utf-8",
    )
    assert ordinal_violations([source]) == []


def test_ordinal_check_reports_location(tmp_path):
    source = tmp_path / "located.py"
    source.write_text("x = 1\ny = 0.5\n", encoding="utf-8")
    violation = ordinal_violations([source])[0]
    assert violation.line == 2
    assert "located.py" in str(violation)
