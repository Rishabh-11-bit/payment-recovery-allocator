"""C5 -- simulator, environment, and arms A and B.

What these guard, in order of how much the result depends on them:

* ground truth never reaches an arm
* the environment enforces regulation, so no arm can benefit from ignoring it
* TERMINAL never recovers via a retry -- definitional, and the primary claim
* runs are deterministic and arms face identical inputs
* the documented baseline is cause-blind, which is what makes it beatable
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from recovery.models import FailureClass
from recovery.sim.arms import ArmA, ArmB, ArmC
from recovery.sim.batch import generate_batch
from recovery.sim.calendar import IST, ComplianceCalendar, calendar_from_config
from recovery.sim.environment import (
    ActionKind,
    CaseOutcome,
    CaseView,
    Environment,
    Proposal,
)
from recovery.sim.metrics import ArmMetrics
from recovery.sim.run import run_arm, run_comparison
from recovery.sim.world import RecoveryCurve, WorldConfigError, load_world_config, sample_world


@pytest.fixture
def world():
    return sample_world(seed=42)


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def small_world():
    raw = load_world_config()
    raw["batch"] = {**raw["batch"], "size": 60}
    return sample_world(seed=7, raw=raw)


# ------------------------------------------------------- ground truth ----- #


def test_case_view_carries_no_ground_truth():
    """If an arm could read the answer, the comparison would measure nothing."""
    leaky = {"true_class", "emission_faithful", "recovery", "hazard"}
    fields = {f.name for f in dataclasses.fields(CaseView)}
    assert not (fields & leaky)


def test_observed_payload_excludes_the_true_class(world):
    for failure in generate_batch(world)[:20]:
        assert "true_class" not in failure.observed()
        assert failure.true_class.value not in str(failure.observed())


def test_emission_is_noisy_so_the_cost_matrix_matters(world):
    """A perfectly separable taxonomy would make misclassification costs decorative."""
    batch = generate_batch(world)
    unfaithful = [f for f in batch if not f.emission_faithful]
    assert unfaithful, "some payloads must misrepresent their class"
    assert len(unfaithful) < len(batch) / 2, "but the signal must still be usable"


# ------------------------------------------------------- determinism ------ #


def test_same_seed_gives_an_identical_world():
    assert sample_world(seed=11) == sample_world(seed=11)


def test_different_seeds_give_different_worlds():
    assert sample_world(seed=11) != sample_world(seed=12)


def test_runs_are_reproducible(small_world, calendar):
    first = run_arm(ArmA(calendar), small_world, calendar)
    second = run_arm(ArmA(calendar), small_world, calendar)
    assert first.as_row() == second.as_row()


def test_arms_face_identical_batches(small_world, calendar):
    result = run_comparison([ArmA(calendar), ArmB(calendar)], small_world, calendar)
    assert result.metrics["A"].cases == result.metrics["B"].cases
    assert result.metrics["A"].money_at_risk_paise == result.metrics["B"].money_at_risk_paise


# ---------------------------------------------------- world sampling ------ #


def test_terminal_recovery_is_identically_zero(world):
    """P(retry succeeds | expired card, cancelled mandate) = 0. Not sampled."""
    curve = world.recovery[FailureClass.TERMINAL]
    assert all(curve.probability(day) == 0.0 for day in range(0, 15))


def test_terminal_recovery_cannot_be_configured_nonzero():
    raw = load_world_config()
    raw["recovery"]["TERMINAL"] = {"base": [0.1, 0.2], "per_day": [0, 0], "cap": [0.3, 0.3]}
    with pytest.raises(WorldConfigError, match="definitional"):
        sample_world(seed=1, raw=raw)


def test_mixes_are_normalised(world):
    assert sum(world.class_mix.values()) == pytest.approx(1.0)
    assert sum(world.rail_mix.values()) == pytest.approx(1.0)


def test_liquidity_recovers_better_later_than_sooner(world):
    """The one ordinal fact the policy is allowed to depend on."""
    curve = world.recovery[FailureClass.LIQUIDITY]
    assert curve.probability(5) > curve.probability(1)


def test_revocation_hazard_compounds_with_notifications(world):
    first = world.revocation_hazard(FailureClass.LIQUIDITY, 1)
    third = world.revocation_hazard(FailureClass.LIQUIDITY, 3)
    assert third > first, "repeated notifications must raise revocation probability"


def test_inverted_range_is_rejected():
    raw = load_world_config()
    raw["emission"]["fidelity"] = [0.9, 0.5]
    with pytest.raises(WorldConfigError, match="inverted"):
        sample_world(seed=1, raw=raw)


def test_recovery_curve_is_clamped():
    curve = RecoveryCurve(base=0.5, per_day=0.5, cap=0.6)
    assert curve.probability(1) == 0.5
    assert curve.probability(10) == 0.6
    assert curve.probability(0) == 0.0


# ------------------------------------------------------ compliance ------- #


def test_no_attempt_lands_in_a_peak_window(small_world, calendar, config):
    """Enforced by the environment, not trusted to the arm."""
    recorded: list[dt.datetime] = []
    original = Environment.submit

    def spy(self, proposal, decided_at, metrics):
        accepted = original(self, proposal, decided_at, metrics)
        if accepted and proposal.kind is ActionKind.ATTEMPT:
            recorded.append(proposal.execute_at)
        return accepted

    Environment.submit = spy
    try:
        run_arm(ArmA(calendar), small_world, calendar)
    finally:
        Environment.submit = original

    assert recorded
    assert not any(calendar.is_peak(moment) for moment in recorded)


def test_peak_windows_are_barred(calendar):
    peak = dt.datetime(2026, 3, 3, 11, 0, tzinfo=IST)
    assert calendar.is_peak(peak)
    violation = calendar.check_attempt(
        decided_at=peak - dt.timedelta(days=2), execute_at=peak, attempts_used=1
    )
    assert violation is not None and "peak_hour_barred" in str(violation)


def test_evening_peak_window_is_barred(calendar):
    assert calendar.is_peak(dt.datetime(2026, 3, 3, 18, 0, tzinfo=IST))
    assert calendar.is_peak(dt.datetime(2026, 3, 3, 21, 0, tzinfo=IST))
    assert not calendar.is_peak(dt.datetime(2026, 3, 3, 22, 0, tzinfo=IST))


def test_pdn_lead_time_is_enforced(calendar):
    execute_at = dt.datetime(2026, 3, 4, 3, 0, tzinfo=IST)
    late = execute_at - dt.timedelta(hours=2)
    violation = calendar.check_attempt(
        decided_at=late, execute_at=execute_at, attempts_used=1
    )
    assert violation is not None and "pdn_lead_time_unmet" in str(violation)


def test_pdn_cutoff_tightens_the_naive_24h(calendar):
    """A PDN at/after 23:50 is rejected when the debit date is T+1."""
    execute_at = dt.datetime(2026, 3, 4, 23, 55, tzinfo=IST)
    naive = execute_at - dt.timedelta(hours=24)
    assert calendar.pdn_deadline_for(execute_at) < naive


def test_attempt_cap_is_enforced(calendar):
    violation = calendar.check_attempt(
        decided_at=dt.datetime(2026, 3, 1, 1, 0, tzinfo=IST),
        execute_at=dt.datetime(2026, 3, 4, 3, 0, tzinfo=IST),
        attempts_used=4,
    )
    assert violation is not None and "attempt_cap_exhausted" in str(violation)


def test_no_case_exceeds_the_attempt_cap(small_world, calendar):
    metrics = run_arm(ArmA(calendar), small_world, calendar)
    # 1 original execution already spent, so at most 3 retries per case.
    assert metrics.attempts_spent <= 3 * metrics.cases


def test_non_compliant_proposals_are_rejected_and_counted(small_world, calendar):
    """A blocked proposal is never silently swallowed."""

    class PeakHourArm:
        name = "peak"

        def propose(self, view, now):
            if view.attempts_used >= 4:
                return []
            return [
                Proposal(
                    case_id=view.case_id,
                    kind=ActionKind.ATTEMPT,
                    execute_at=(now + dt.timedelta(days=2)).replace(hour=11, minute=0),
                    note="deliberately non-compliant",
                )
            ]

    metrics = run_arm(PeakHourArm(), small_world, calendar)
    assert metrics.attempts_spent == 0
    assert metrics.proposals_rejected > 0
    assert "peak_hour_barred" in metrics.rejection_reasons


# --------------------------------------------- bank-holiday shifting ------ #


def test_emandate_charge_day_shifts_off_a_holiday():
    holiday = dt.date(2026, 3, 5)
    calendar = ComplianceCalendar(
        peak_windows=(),
        pdn_lead_time_hours=24,
        pdn_cutoff=dt.time(23, 50),
        attempt_cap=4,
        bank_holidays=frozenset({holiday}),
    )
    assert calendar.shift_for_bank_holidays(holiday) == dt.date(2026, 3, 4)


def test_two_consecutive_holidays_shift_to_t_minus_3():
    first, second = dt.date(2026, 3, 4), dt.date(2026, 3, 5)
    calendar = ComplianceCalendar(
        peak_windows=(),
        pdn_lead_time_hours=24,
        pdn_cutoff=dt.time(23, 50),
        attempt_cap=4,
        bank_holidays=frozenset({first, second}),
    )
    assert calendar.shift_for_bank_holidays(second) == second - dt.timedelta(days=3)


def test_sundays_count_as_bank_holidays(calendar):
    sunday = dt.date(2026, 3, 8)
    assert sunday.weekday() == 6
    assert calendar.is_bank_holiday(sunday)


# ------------------------------------------------------------- arms ------ #


def test_arm_a_is_cause_blind(world, calendar):
    """Expired card and insufficient balance get identical treatment."""
    arm = ArmA(calendar)
    now = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
    failed_at = dt.datetime(2026, 3, 2, 3, 0, tzinfo=IST)

    def view_for(reason: str) -> CaseView:
        return CaseView(
            case_id="c",
            rail="card",
            amount_paise=49900,
            failed_at=failed_at,
            observed={"error_reason": reason, "method": "card"},
            attempts_used=1,
            contacts_used=0,
            outcome=CaseOutcome.OPEN,
            attempt_pending=False,
            last_attempt_resolved_at=None,
        )

    dead_card = arm.propose(view_for("payment_expired_card"), now)
    broke = arm.propose(view_for("insufficient_funds"), now)

    assert [p.execute_at for p in dead_card] == [p.execute_at for p in broke]


def test_arm_a_stops_at_three_retries(calendar):
    arm = ArmA(calendar)
    view = CaseView(
        case_id="c",
        rail="card",
        amount_paise=49900,
        failed_at=dt.datetime(2026, 3, 2, 3, 0, tzinfo=IST),
        observed={},
        attempts_used=4,
        contacts_used=0,
        outcome=CaseOutcome.OPEN,
        attempt_pending=False,
        last_attempt_resolved_at=None,
    )
    assert arm.propose(view, dt.datetime(2026, 3, 5, 1, 0, tzinfo=IST)) == []


def test_arm_a_waits_for_emandate_confirmation(calendar):
    """Async: no retry until the previous attempt is confirmed or rejected."""
    arm = ArmA(calendar)
    view = CaseView(
        case_id="c",
        rail="emandate",
        amount_paise=49900,
        failed_at=dt.datetime(2026, 3, 2, 3, 0, tzinfo=IST),
        observed={},
        attempts_used=2,
        contacts_used=0,
        outcome=CaseOutcome.OPEN,
        attempt_pending=True,
        last_attempt_resolved_at=None,
    )
    assert arm.propose(view, dt.datetime(2026, 3, 3, 1, 0, tzinfo=IST)) == []


def test_arm_a_sends_no_contacts(small_world, calendar):
    metrics = run_arm(ArmA(calendar), small_world, calendar)
    assert metrics.contacts_sent == 0


def test_arm_b_contacts_every_case_exactly_once(small_world, calendar):
    metrics = run_arm(ArmB(calendar), small_world, calendar)
    assert metrics.contacts_sent == metrics.cases


def test_arm_b_contacts_terminal_cases_too(small_world, calendar):
    """No cause awareness: a dead card gets a link like everything else."""
    metrics = run_arm(ArmB(calendar), small_world, calendar)
    assert metrics.terminal_contacts_sent > 0


def test_arm_c_is_not_implemented(calendar):
    with pytest.raises(NotImplementedError, match="hand-authored"):
        ArmC(calendar).propose(None, dt.datetime.now(IST))


# --------------------------------------------------------- outcomes ------ #


def test_terminal_cases_never_recover_by_retry(small_world, calendar):
    """Definitional, and the basis of the primary claim."""
    batch = generate_batch(small_world)
    terminal = [f for f in batch if f.true_class is FailureClass.TERMINAL]
    assert terminal

    environment = Environment(small_world, batch, calendar)
    metrics = ArmMetrics(arm="probe")
    now = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)

    for failure in terminal:
        for offset in (1, 2, 3):
            environment.submit(
                Proposal(
                    case_id=failure.case_id,
                    kind=ActionKind.ATTEMPT,
                    execute_at=dt.datetime(2026, 3, 2 + offset, 3, 0, tzinfo=IST),
                ),
                now,
                metrics,
            )

    assert metrics.money_recovered_paise == 0
    assert metrics.terminal_attempts_wasted == metrics.attempts_spent


def test_every_case_ends_in_exactly_one_terminal_state(small_world, calendar):
    metrics = run_arm(ArmB(calendar), small_world, calendar)
    assert metrics.mandates_preserved + metrics.mandates_revoked == metrics.cases


def test_contacts_are_priced_when_a_cost_model_is_supplied(small_world, calendar, classifier):
    metrics = run_arm(
        ArmB(calendar), small_world, calendar, costs=classifier.config.costs
    )
    assert metrics.contact_cost_incurred > 0


def test_terminal_waste_counts_only_terminal_cases(small_world, calendar):
    metrics = run_arm(ArmA(calendar), small_world, calendar)
    assert metrics.terminal_attempts_wasted == metrics.attempts_by_class.get("TERMINAL", 0)
    assert metrics.terminal_attempts_wasted < metrics.attempts_spent


def test_baseline_spends_attempts_on_unrecoverable_failures(small_world, calendar):
    """The thesis, measured: the cause-blind baseline burns budget on dead cases."""
    metrics = run_arm(ArmA(calendar), small_world, calendar)
    assert metrics.terminal_attempts_wasted > 0
    assert metrics.wasted_attempt_share > 0
