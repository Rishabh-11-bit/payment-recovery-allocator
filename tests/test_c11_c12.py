"""C11 storm governor and C12 holdout harness.

The governor's load-bearing property is that it holds no list of issuers: the
NPCI data shows outages rotate, and the second persistent bank looked fine in
April (CHALLENGES 011). A blocklist built from any one month misses it.

The holdout's load-bearing property is that assignment is deterministic and
stratified, so a replay cannot move a case between arms and an unbalanced draw
cannot masquerade as an effect.
"""

from __future__ import annotations

import datetime as dt

import pytest

from recovery.governor import (
    IssuerState,
    StormGovernor,
    governor_from_config,
)
from recovery.holdout import (
    CONTROL,
    TREATMENT,
    HoldoutResult,
    Outcome,
    assign,
    measure,
    stratum_of,
)
from recovery.sim.calendar import IST
from recovery.sim.world import sample_world

NOW = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)


def load(governor: StormGovernor, issuer: str, *, failures: int, total: int, at=NOW):
    for index in range(total):
        governor.observe(issuer, at + dt.timedelta(minutes=index), failed=index < failures)


# --------------------------------------------------------------------------- #
# C11 -- no static list
# --------------------------------------------------------------------------- #


def test_governor_starts_knowing_no_issuers():
    """It cannot hold a blocklist: issuers exist only once observed."""
    assert StormGovernor().known_issuers() == []


def test_a_healthy_issuer_that_degrades_gets_throttled():
    """Punjab National Bank was fine in April and five times worse by June."""
    governor = StormGovernor(min_observations=10)
    load(governor, "bank_x", failures=0, total=20)
    assert governor.state_of("bank_x", NOW) is IssuerState.HEALTHY
    healthy_ceiling = governor.ceiling_for("bank_x", NOW)

    later = NOW + dt.timedelta(hours=30)  # past the window; the old record ages out
    load(governor, "bank_x", failures=20, total=20, at=later)

    assert governor.state_of("bank_x", later) is IssuerState.DEGRADED
    assert governor.ceiling_for("bank_x", later) < healthy_ceiling


def test_a_degraded_issuer_that_recovers_is_released():
    """A blocklist needs manual removal. Observed conditions do not."""
    governor = StormGovernor(min_observations=10)
    load(governor, "bank_y", failures=20, total=20)
    assert governor.state_of("bank_y", NOW) is IssuerState.DEGRADED

    later = NOW + dt.timedelta(hours=30)
    load(governor, "bank_y", failures=0, total=20, at=later)
    assert governor.state_of("bank_y", later) is IssuerState.HEALTHY


def test_an_unmeasured_issuer_is_not_assumed_healthy_on_thin_evidence():
    """Below the observation floor there is no measurement, only a hunch."""
    governor = StormGovernor(min_observations=10)
    load(governor, "bank_z", failures=3, total=3)
    assert governor.failure_share("bank_z", NOW) is None


NAMED_BANKS = ("central bank", "punjab national", "sbi", "hdfc", "icici", "axis")


def test_no_issuer_name_appears_in_governor_code():
    """The design consequence of CHALLENGES 011, enforced rather than intended.

    AST rather than a text scan, and docstrings are excluded deliberately: the
    module docstring *explains* why there is no list, and naming a bank there is
    the argument. Naming one in a data structure would be the failure.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("recovery/governor.py").read_text(encoding="utf-8"))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
        and any(name in node.value.lower() for name in NAMED_BANKS)
    ]
    assert not offenders, f"governor names an issuer in code: {offenders}"


def test_no_issuer_name_appears_in_governor_config():
    import pathlib

    block = (
        pathlib.Path("config/default.yaml")
        .read_text(encoding="utf-8")
        .lower()
        .split("governor:")[-1]
    )
    for name in NAMED_BANKS:
        for line in block.splitlines():
            if name in line:
                assert line.strip().startswith("#"), (
                    f"config names {name!r} outside a comment: {line.strip()}"
                )


# ---------------------------------------------------------------- ceiling -- #


def test_ceiling_holds_admissions_once_reached():
    governor = StormGovernor(base_ceiling=3, min_observations=100)
    verdicts = [governor.admit("bank_a", NOW) for _ in range(5)]
    assert [v.admitted for v in verdicts] == [True, True, True, False, False]
    assert "ceiling reached" in verdicts[-1].reason


def test_ceiling_releases_as_the_window_slides():
    governor = StormGovernor(base_ceiling=2, window_hours=1, min_observations=100)
    assert governor.admit("bank_b", NOW).admitted
    assert governor.admit("bank_b", NOW).admitted
    assert not governor.admit("bank_b", NOW).admitted
    assert governor.admit("bank_b", NOW + dt.timedelta(hours=2)).admitted


def test_a_degraded_issuer_gets_a_tighter_ceiling():
    governor = StormGovernor(min_observations=10, base_ceiling=50, degraded_ceiling=2)
    load(governor, "bank_c", failures=20, total=20)
    admitted = [governor.admit("bank_c", NOW).admitted for _ in range(5)]
    assert sum(admitted) == 2


def test_ceilings_are_per_issuer_not_global():
    governor = StormGovernor(base_ceiling=1, min_observations=100)
    assert governor.admit("bank_d", NOW).admitted
    assert governor.admit("bank_e", NOW).admitted, "one issuer's storm blocked another"


def test_dry_run_admission_records_nothing():
    governor = StormGovernor(base_ceiling=1, min_observations=100)
    assert governor.admit("bank_f", NOW, record=False).admitted
    assert governor.admit("bank_f", NOW).admitted


# ----------------------------------------------------------------- jitter -- #


def test_jitter_is_deterministic_for_a_key():
    """A retried worker must not reschedule the execution somewhere else."""
    governor = StormGovernor()
    first = governor.jitter(NOW, "case_1")
    assert first == governor.jitter(NOW, "case_1")


def test_jitter_spreads_different_keys():
    governor = StormGovernor(jitter_minutes=45)
    slots = {governor.jitter(NOW, f"case_{i}") for i in range(60)}
    assert len(slots) > 20, "jitter is stacking cases on the same minute"


def test_jitter_only_moves_forward():
    """Moving earlier could cross the PDN lead time or enter a peak window."""
    governor = StormGovernor(jitter_minutes=45)
    for index in range(200):
        assert governor.jitter(NOW, f"case_{index}") >= NOW


def test_jitter_stays_inside_its_configured_spread():
    governor = StormGovernor(jitter_minutes=30)
    for index in range(200):
        assert governor.jitter(NOW, f"c{index}") <= NOW + dt.timedelta(minutes=30)


# ------------------------------------------------------------- from config - #


def test_thresholds_come_from_the_sourced_outage_distribution(config):
    """"Degraded" means worse than what NPCI published, not a number picked."""
    world = sample_world(seed=42)
    governor = governor_from_config(config, world)
    outage = world.issuer_outage

    assert governor.degraded_failure_share == pytest.approx(
        min(1.0, outage["per_bank_share"][1] * config.governor.sourced_multiplier)
    )
    assert governor.strained_failure_share < governor.degraded_failure_share


def test_falls_back_to_config_when_no_sourced_outage(config):
    governor = governor_from_config(config, None)
    assert governor.strained_failure_share == config.governor.strained_failure_share


def test_ceilings_must_tighten_as_conditions_worsen(tmp_path):
    import yaml
    from pydantic import ValidationError

    from recovery.config import load_config

    data = yaml.safe_load(
        __import__("pathlib").Path("config/default.yaml").read_text(encoding="utf-8")
    )
    data["governor"]["degraded_ceiling"] = 999
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="tighten"):
        load_config(path)


# --------------------------------------------------------------------------- #
# C12 -- holdout
# --------------------------------------------------------------------------- #


def test_assignment_is_deterministic():
    """A replayed webhook must not move a case between arms mid-experiment."""
    kwargs = dict(rail="upi", failure_class="LIQUIDITY", experiment="e1",
                  control_fraction=0.1)
    assert assign("order_1", **kwargs).arm == assign("order_1", **kwargs).arm


def test_assignment_needs_no_stored_state():
    """It survives a restart because there is nothing to survive."""
    kwargs = dict(rail="upi", failure_class="LIQUIDITY", experiment="e1",
                  control_fraction=0.1)
    first = assign("order_x", **kwargs)
    assert first.arm == assign("order_x", **kwargs).arm
    assert first.bucket == assign("order_x", **kwargs).bucket


def test_a_different_experiment_reshuffles_everybody():
    """Otherwise a second experiment is silently correlated with the first."""
    common = dict(rail="upi", failure_class="LIQUIDITY", control_fraction=0.5)
    first = {
        f"o{i}": assign(f"o{i}", experiment="e1", **common).arm for i in range(300)
    }
    second = {
        f"o{i}": assign(f"o{i}", experiment="e2", **common).arm for i in range(300)
    }
    agreement = sum(1 for k in first if first[k] == second[k]) / len(first)
    assert 0.35 < agreement < 0.65, f"experiments are correlated: {agreement:.0%}"


def test_control_fraction_is_respected():
    arms = [
        assign(f"order_{i}", rail="upi", failure_class="LIQUIDITY",
               experiment="e1", control_fraction=0.2).arm
        for i in range(4000)
    ]
    share = arms.count(CONTROL) / len(arms)
    assert 0.17 < share < 0.23, share


def test_each_stratum_is_split_at_the_target_fraction():
    """An unstratified draw can hand the control a TERMINAL-heavy sample."""
    for rail in ("upi", "card", "emandate"):
        for failure_class in ("LIQUIDITY", "TERMINAL", "ATTENTION", "INFRASTRUCTURE"):
            arms = [
                assign(f"order_{i}", rail=rail, failure_class=failure_class,
                       experiment="e1", control_fraction=0.25).arm
                for i in range(1200)
            ]
            share = arms.count(CONTROL) / len(arms)
            assert 0.20 < share < 0.30, f"{rail}/{failure_class}: {share:.2%}"


def test_zero_and_full_fractions():
    common = dict(rail="upi", failure_class="LIQUIDITY", experiment="e1")
    assert assign("o", control_fraction=0.0, **common).arm == TREATMENT
    assert assign("o", control_fraction=1.0, **common).arm == CONTROL


def test_invalid_fraction_is_rejected():
    with pytest.raises(ValueError):
        assign("o", rail="upi", failure_class="X", experiment="e", control_fraction=1.5)


# ----------------------------------------------------------- measurement -- #


def outcome(arm, stratum, recovered, revoked=False):
    return Outcome(
        chain_key="o", arm=arm, stratum=stratum, recovered_paise=recovered,
        executions_spent=1, contacts_sent=0, mandate_revoked=revoked,
    )


def test_uplift_is_computed_from_realised_outcomes():
    result = measure("e1", [
        outcome(CONTROL, "upi:LIQUIDITY", 0),
        outcome(CONTROL, "upi:LIQUIDITY", 0),
        outcome(TREATMENT, "upi:LIQUIDITY", 10000),
        outcome(TREATMENT, "upi:LIQUIDITY", 10000),
    ])
    assert result.uplift_per_case_paise == pytest.approx(10000)


def test_uplift_is_stratum_weighted_not_pooled():
    """Pooling lets a stratum-size imbalance masquerade as an effect.

    Stratum `a` is mostly control and recovers nothing; stratum `b` is mostly
    treatment and recovers a lot. Pooling reads that allocation imbalance as an
    effect. Weighting by stratum size does not.
    """
    outcomes = [outcome(CONTROL, "a", 0) for _ in range(100)]
    outcomes += [outcome(TREATMENT, "a", 0) for _ in range(10)]
    outcomes += [outcome(CONTROL, "b", 0) for _ in range(2)]
    outcomes += [outcome(TREATMENT, "b", 10000) for _ in range(90)]

    result = measure("e1", outcomes)
    pooled = (
        result.treatment_recovered_paise / result.treatment_cases
        - result.control_recovered_paise / result.control_cases
    )
    assert result.uplift_per_case_paise < pooled
    assert pooled > 8000, "the pooled figure should look impressive"
    assert result.uplift_per_case_paise < 6000, "the weighted one should not"


def test_empty_sided_strata_are_named_not_silently_included():
    result = measure("e1", [
        outcome(CONTROL, "upi:LIQUIDITY", 0),
        outcome(TREATMENT, "upi:LIQUIDITY", 100),
        outcome(TREATMENT, "card:TERMINAL", 500),
    ])
    assert result.underpowered_strata == ("card:TERMINAL",)
    assert "arithmetic rather than evidence" in result.describe()


def test_revocations_are_reported_per_arm():
    """The mandate-survival side of the claim needs measuring too."""
    result = measure("e1", [
        outcome(CONTROL, "s", 0, revoked=True),
        outcome(TREATMENT, "s", 0, revoked=False),
    ])
    assert result.control_revoked == 1
    assert result.treatment_revoked == 0


def test_output_refuses_to_imply_significance():
    result = measure("e1", [outcome(CONTROL, "s", 0), outcome(TREATMENT, "s", 100)])
    described = result.describe()
    assert "NOT a significance test" in described
    assert "power calculation" in described


def test_empty_experiment_measures_nothing():
    result = measure("e1", [])
    assert result.uplift_per_case_paise == 0.0
    assert result.control_cases == 0


def test_stratum_label_is_rail_and_class():
    assert stratum_of("upi", "LIQUIDITY") == "upi:LIQUIDITY"
