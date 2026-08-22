"""C2 machinery.

These test the machinery, not the taxonomy. The mapping in
`config/classifier.yaml` is a stub and its rows are illustrative, so nothing
here asserts that a particular key *should* be a particular class -- that is
authored content and its tests belong with it. What is asserted is that the
loader is strict, the ladder bands correctly, unmapped keys are never silently
defaulted, and low confidence resolves toward the cheaper error.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from recovery.classifier import (
    Classifier,
    ClassifierConfigError,
    CostModel,
    load_classifier,
)
from recovery.fixtures import (
    CAPTURED_CUSTOMER_CANCELLED,
    CAPTURED_GENERIC_DECLINE,
    CAPTURED_MERCHANT_CONFIG_TERMINAL,
    load_captured_payments,
)
from recovery.models import ConfidenceBand, FailureClass, NormalizedFailure
from recovery.normalize import normalize_entity

SHIPPED = pathlib.Path("config/classifier.yaml")


def write_config(tmp_path: pathlib.Path, **overrides) -> pathlib.Path:
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        data[key] = value
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "classifier.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def key(method=None, source=None, step=None, reason=None, **kwargs) -> NormalizedFailure:
    return NormalizedFailure(
        method=method, source=source, step=step, reason=reason, **kwargs
    )


# ------------------------------------------------------------ stub gate ---- #


def test_stub_config_refuses_to_load_by_default():
    """An unfinished taxonomy must not quietly produce results."""
    with pytest.raises(ClassifierConfigError, match="STUB"):
        load_classifier(SHIPPED)


def test_stub_config_loads_when_opted_into():
    assert load_classifier(SHIPPED, allow_stub=True) is not None


def test_authored_config_needs_no_opt_in(tmp_path):
    path = write_config(tmp_path, status="AUTHORED")
    assert load_classifier(path) is not None


# --------------------------------------------------------- loader rigour --- #


def test_rule_matching_on_an_unknown_field_is_rejected(tmp_path):
    path = write_config(
        tmp_path, rules=[{"match": {"methodd": "upi"}, "class": "LIQUIDITY", "confidence": 0.9}]
    )
    with pytest.raises(ClassifierConfigError, match="unknown field"):
        load_classifier(path, allow_stub=True)


def test_empty_match_is_rejected(tmp_path):
    """A catch-all rule is what `fallback` is for."""
    path = write_config(tmp_path, rules=[{"match": {}, "class": "LIQUIDITY", "confidence": 0.9}])
    with pytest.raises(ClassifierConfigError, match="empty match"):
        load_classifier(path, allow_stub=True)


def test_ambiguous_rules_are_rejected_at_load(tmp_path):
    """Equal specificity, overlapping, disagreeing -- a coin toss at runtime."""
    path = write_config(
        tmp_path,
        rules=[
            {"match": {"method": "upi", "step": "s"}, "class": "LIQUIDITY", "confidence": 0.9},
            {
                "match": {"method": "upi", "source": "customer"},
                "class": "TERMINAL",
                "confidence": 0.9,
            },
        ],
    )
    with pytest.raises(ClassifierConfigError, match="disagree"):
        load_classifier(path, allow_stub=True)


def test_overlapping_rules_that_agree_are_allowed(tmp_path):
    path = write_config(
        tmp_path,
        rules=[
            {"match": {"method": "upi", "step": "s"}, "class": "LIQUIDITY", "confidence": 0.9},
            {
                "match": {"method": "upi", "source": "customer"},
                "class": "LIQUIDITY",
                "confidence": 0.9,
            },
        ],
    )
    assert load_classifier(path, allow_stub=True) is not None


def test_confidence_outside_unit_interval_is_rejected(tmp_path):
    path = write_config(
        tmp_path, rules=[{"match": {"method": "upi"}, "class": "LIQUIDITY", "confidence": 1.4}]
    )
    with pytest.raises(ClassifierConfigError, match="outside"):
        load_classifier(path, allow_stub=True)


def test_bands_must_be_ordered(tmp_path):
    path = write_config(tmp_path, confidence_bands={"high": 0.5, "moderate": 0.9})
    with pytest.raises(ClassifierConfigError, match="moderate"):
        load_classifier(path, allow_stub=True)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ({"INFRASTRUCTURE": {"INFRASTRUCTURE": 5}}, "must be 0"),
        ({"NOT_A_CLASS": {}}, "unknown class"),
    ],
)
def test_cost_matrix_is_validated(tmp_path, mutation, expected):
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    for row, columns in mutation.items():
        data["costs"]["misclassification"].setdefault(row, {}).update(columns)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ClassifierConfigError, match=expected):
        load_classifier(path, allow_stub=True)


def test_incomplete_cost_matrix_is_rejected(tmp_path):
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    del data["costs"]["misclassification"]["TERMINAL"]
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ClassifierConfigError, match="missing row"):
        load_classifier(path, allow_stub=True)


# ------------------------------------------------------ specificity ------- #


def test_more_specific_rule_wins(tmp_path):
    path = write_config(
        tmp_path,
        rules=[
            {"match": {"method": "upi"}, "class": "INFRASTRUCTURE", "confidence": 0.9},
            {
                "match": {"method": "upi", "step": "payment_authentication"},
                "class": "ATTENTION",
                "confidence": 0.9,
            },
        ],
    )
    classifier = load_classifier(path, allow_stub=True)
    result = classifier.classify(key(method="upi", step="payment_authentication"))

    assert result.failure_class is FailureClass.ATTENTION


def test_declaration_order_does_not_change_the_outcome(tmp_path):
    rules = [
        {
            "match": {"method": "upi", "step": "payment_authentication"},
            "class": "ATTENTION",
            "confidence": 0.9,
        },
        {"match": {"method": "upi"}, "class": "INFRASTRUCTURE", "confidence": 0.9},
    ]
    forward = load_classifier(write_config(tmp_path / "a", rules=rules), allow_stub=True)
    reverse = load_classifier(
        write_config(tmp_path / "b", rules=list(reversed(rules))), allow_stub=True
    )
    probe = key(method="upi", step="payment_authentication")

    assert forward.classify(probe).failure_class is reverse.classify(probe).failure_class


# -------------------------------------------------- confidence ladder ----- #


@pytest.mark.parametrize(
    "confidence, expected",
    [
        (1.00, ConfidenceBand.HIGH),
        (0.85, ConfidenceBand.HIGH),
        (0.84, ConfidenceBand.MODERATE),
        (0.60, ConfidenceBand.MODERATE),
        (0.59, ConfidenceBand.LOW),
        (0.00, ConfidenceBand.LOW),
    ],
)
def test_band_boundaries_are_inclusive_at_the_threshold(classifier, confidence, expected):
    assert classifier.band_for(confidence) is expected


def test_only_high_band_permits_exclusion(tmp_path):
    """Excluding on a misdiagnosis makes recovery harder; reorder is the default."""
    path = write_config(
        tmp_path,
        rules=[
            {"match": {"method": "card"}, "class": "TERMINAL", "confidence": 0.95},
            {"match": {"method": "upi"}, "class": "TERMINAL", "confidence": 0.70},
        ],
    )
    classifier = load_classifier(path, allow_stub=True)

    assert classifier.classify(key(method="card")).may_exclude_instrument is True
    assert classifier.classify(key(method="upi")).may_exclude_instrument is False


# ------------------------------------------------------ fallback path ----- #


def test_unmapped_key_falls_back_and_is_marked(classifier):
    result = classifier.classify(key(method="upi", step="a_step_nobody_mapped"))

    assert result.mapped is False, "the fallback must be distinguishable from a real match"
    assert result.rule_index is None
    assert result.confidence == 0.0
    assert result.band is ConfidenceBand.LOW


def test_fallback_confidence_never_looks_authoritative(classifier):
    """The point of the fallback is that it does not masquerade as knowledge."""
    result = classifier.classify(key(method="netbanking", step="unknown"))

    assert result.band is ConfidenceBand.LOW
    assert result.may_exclude_instrument is False


# ----------------------------------------- method-partitioned value space -- #


def test_source_outside_its_methods_space_is_not_trusted(classifier):
    """`customer_psp` is a UPI source. On a card payment it is anomalous."""
    probe = key(
        method="card",
        source="customer_psp",
        step="payment_debit_response",
        source_in_documented_space=False,
    )
    assert classifier.classify(probe).mapped is False


def test_no_razorpay_source_exists(classifier):
    space = classifier.config.source_space
    assert all("razorpay" not in sources for sources in space.values())
    assert "internal" in space["card"]


def test_bare_bank_source_covers_netbanking_on_captured_evidence(classifier):
    """This assertion used to exclude netbanking, on the documentation's word.

    A captured test-mode netbanking failure returns `source: bank`. The
    reference lists a bare `bank` only for emandate, so the documentation is a
    subset of what the API emits and the test was encoding the doc rather than
    reality. See CHALLENGES 007.
    """
    space = classifier.config.source_space
    assert "bank" in space["emandate"]
    assert "bank" in space["netbanking"]
    assert all("bank" not in space[method] for method in ("card", "upi"))


def test_upi_extends_the_card_source_space(classifier):
    space = classifier.config.source_space
    assert space["card"] <= space["upi"]
    assert {"customer_psp", "network", "beneficiary_bank"} <= space["upi"]


# ------------------------------------------------- cost-based resolution -- #


def test_low_confidence_resolves_toward_the_cheaper_error(tmp_path):
    """Not toward the more likely class -- toward the least costly mistake."""
    path = write_config(
        tmp_path,
        rules=[{"match": {"method": "upi"}, "class": "TERMINAL", "confidence": 0.10}],
        costs={"contact": {c: 1 for c in ("INFRASTRUCTURE","LIQUIDITY","ATTENTION","TERMINAL")}, "misclassification": {
            # TERMINAL is ruinous to predict wrongly; ATTENTION is cheap.
            "INFRASTRUCTURE": {"INFRASTRUCTURE": 0, "LIQUIDITY": 2, "ATTENTION": 1, "TERMINAL": 90},
            "LIQUIDITY": {"INFRASTRUCTURE": 2, "LIQUIDITY": 0, "ATTENTION": 1, "TERMINAL": 90},
            "ATTENTION": {"INFRASTRUCTURE": 2, "LIQUIDITY": 2, "ATTENTION": 0, "TERMINAL": 90},
            "TERMINAL": {"INFRASTRUCTURE": 3, "LIQUIDITY": 3, "ATTENTION": 2, "TERMINAL": 0},
        }},
    )
    classifier = load_classifier(path, allow_stub=True)
    result = classifier.classify(key(method="upi"))

    assert result.cost_resolved_from is FailureClass.TERMINAL
    assert result.failure_class is FailureClass.ATTENTION
    assert result.confidence == 0.10, "the low confidence is reported, not laundered"


def test_high_confidence_is_not_second_guessed(tmp_path):
    path = write_config(
        tmp_path,
        rules=[{"match": {"method": "upi"}, "class": "TERMINAL", "confidence": 0.99}],
    )
    result = load_classifier(path, allow_stub=True).classify(key(method="upi"))

    assert result.failure_class is FailureClass.TERMINAL
    assert result.cost_resolved_from is None


def test_safest_class_is_minimax_not_most_likely():
    matrix = CostModel(
        contact={c: 1.0 for c in FailureClass},
        misclassification={
            FailureClass.INFRASTRUCTURE: {
                FailureClass.INFRASTRUCTURE: 0,
                FailureClass.LIQUIDITY: 1,
                FailureClass.ATTENTION: 1,
                FailureClass.TERMINAL: 50,
            },
            FailureClass.LIQUIDITY: {
                FailureClass.INFRASTRUCTURE: 1,
                FailureClass.LIQUIDITY: 0,
                FailureClass.ATTENTION: 1,
                FailureClass.TERMINAL: 50,
            },
            FailureClass.ATTENTION: {
                FailureClass.INFRASTRUCTURE: 1,
                FailureClass.LIQUIDITY: 1,
                FailureClass.ATTENTION: 0,
                FailureClass.TERMINAL: 50,
            },
            FailureClass.TERMINAL: {
                FailureClass.INFRASTRUCTURE: 2,
                FailureClass.LIQUIDITY: 2,
                FailureClass.ATTENTION: 2,
                FailureClass.TERMINAL: 0,
            },
        },
    )
    assert matrix.worst_case(FailureClass.TERMINAL) == 50
    assert matrix.safest_class() is not FailureClass.TERMINAL


def test_safest_class_is_deterministic_under_ties():
    flat = {
        true: {predicted: (0 if true is predicted else 5) for predicted in FailureClass}
        for true in FailureClass
    }
    matrix = CostModel(misclassification=flat, contact={c: 1.0 for c in FailureClass})
    assert len({matrix.safest_class() for _ in range(20)}) == 1


# ---------------------------------------------------------- determinism --- #


def test_classification_is_deterministic(classifier):
    probe = key(method="upi", source="customer_psp", step="payment_debit_response")
    results = {classifier.classify(probe).failure_class for _ in range(50)}
    assert len(results) == 1


def test_no_model_and_no_network(classifier):
    """C2 is a lookup table. Nothing here may call out."""
    assert isinstance(classifier, Classifier)
    assert classifier.config.rules, "rules come from config, not from code"


# ------------------------------------------------- cost-model schema ------ #


def test_fallback_class_is_rejected_if_configured(tmp_path):
    """A safe default written separately from the matrix would drift from it."""
    path = write_config(tmp_path, fallback={"class": "TERMINAL", "confidence": 0.0})
    with pytest.raises(ClassifierConfigError, match="not configurable"):
        load_classifier(path, allow_stub=True)


def test_unmapped_resolves_through_the_cost_model(tmp_path):
    """Change the costs and the fallback follows -- no second place to edit."""
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    data["costs"]["misclassification"] = {
        "INFRASTRUCTURE": {"INFRASTRUCTURE": 0, "LIQUIDITY": 9, "ATTENTION": 9, "TERMINAL": 9},
        "LIQUIDITY": {"INFRASTRUCTURE": 1, "LIQUIDITY": 0, "ATTENTION": 9, "TERMINAL": 9},
        "ATTENTION": {"INFRASTRUCTURE": 1, "LIQUIDITY": 9, "ATTENTION": 0, "TERMINAL": 9},
        "TERMINAL": {"INFRASTRUCTURE": 1, "LIQUIDITY": 9, "ATTENTION": 9, "TERMINAL": 0},
    }
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    classifier = load_classifier(path, allow_stub=True)

    result = classifier.classify(key(method="upi", step="nothing_maps_this"))
    assert result.failure_class is FailureClass.INFRASTRUCTURE
    assert result.mapped is False


def test_contact_costs_are_required(tmp_path):
    """A contact costs something even when the class is right."""
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    del data["costs"]["contact"]
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ClassifierConfigError, match="costs.contact"):
        load_classifier(path, allow_stub=True)


def test_incomplete_contact_costs_are_rejected(tmp_path):
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    del data["costs"]["contact"]["TERMINAL"]
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ClassifierConfigError, match="missing entries"):
        load_classifier(path, allow_stub=True)


def test_contact_cost_is_readable_per_class(classifier):
    costs = classifier.config.costs
    assert all(costs.contact_cost(c) >= 0 for c in FailureClass)


# ------------------------------------------- captured payloads (real) ----- #


def test_every_captured_payload_classifies(classifier):
    """No real capture may be rejected. The doc is a subset of reality."""
    captured = load_captured_payments()
    if not captured:
        pytest.skip("no captured payments committed yet")

    for entity in captured:
        result = classifier.classify(
            normalize_entity(entity, source_space=classifier.config.source_space)
        )
        assert result.failure_class is not None
        assert 0.0 <= result.confidence <= 1.0


def test_undocumented_source_is_surfaced_not_rejected(classifier):
    """The netbanking `bank` case: flagged for review, classified anyway."""
    result = classifier.classify(
        normalize_entity(
            CAPTURED_GENERIC_DECLINE, source_space=classifier.config.source_space
        )
    )
    assert result.source_undocumented is False, "bank is now documented for netbanking"

    # A source genuinely outside its space still classifies, and still flags.
    off_space = classifier.classify(
        normalize_entity(
            {**CAPTURED_GENERIC_DECLINE, "method": "card"},
            source_space=classifier.config.source_space,
        )
    )
    assert off_space.source_undocumented is True
    assert off_space.failure_class is not None, "surfaced, not rejected"


def test_undocumented_source_does_not_cost_confidence(tmp_path):
    """Surfacing is not a penalty: the classification stands on its own."""
    path = write_config(
        tmp_path,
        rules=[{"match": {"step": "payment_authorization"}, "class": "LIQUIDITY",
                "confidence": 0.90}],
    )
    classifier = load_classifier(path, allow_stub=True)
    documented = classifier.classify(key(method="netbanking", source="customer",
                                         step="payment_authorization"))
    undocumented = classifier.classify(
        key(method="netbanking", source="martian_bank", step="payment_authorization",
            source_in_documented_space=False)
    )
    assert undocumented.confidence == documented.confidence
    assert undocumented.failure_class is documented.failure_class
    assert undocumented.source_undocumented is True


def test_generic_decline_is_the_canonical_low_confidence_case(classifier):
    """`payment_failed` from `bank` carries no information. Do not pretend it does."""
    result = classifier.classify(
        normalize_entity(
            CAPTURED_GENERIC_DECLINE, source_space=classifier.config.source_space
        )
    )
    assert result.band is ConfidenceBand.LOW
    assert result.may_exclude_instrument is False, "must not exclude on no information"
    assert result.cost_resolved_from is not None, "LOW hands the decision to the cost model"


def test_merchant_configuration_terminal_is_expressible(classifier):
    """TERMINAL for a merchant reason, distinguishable from an instrument one."""
    result = classifier.classify(
        normalize_entity(
            CAPTURED_MERCHANT_CONFIG_TERMINAL, source_space=classifier.config.source_space
        )
    )
    assert result.failure_class is FailureClass.TERMINAL
    assert result.cause_family == "merchant_configuration"

    expired = classifier.classify(
        key(method="card", source="issuer_bank", reason="payment_expired_card")
    )
    assert expired.failure_class is FailureClass.TERMINAL
    assert expired.cause_family == "instrument"
    # Same class, same zero retry probability, different remedy.
    assert expired.cause_family != result.cause_family


def test_cause_family_is_optional(classifier):
    result = classifier.classify(key(method="upi", step="payment_initiation"))
    assert result.cause_family is None


def test_empty_cause_family_is_rejected(tmp_path):
    path = write_config(
        tmp_path,
        rules=[{"match": {"method": "upi"}, "class": "LIQUIDITY", "confidence": 0.9,
                "cause_family": "   "}],
    )
    with pytest.raises(ClassifierConfigError, match="cause_family"):
        load_classifier(path, allow_stub=True)


def test_wallet_method_has_no_value_space_and_is_not_flagged(classifier):
    """A method the table does not cover at all must not be treated as anomalous."""
    result = classifier.classify(
        normalize_entity(
            CAPTURED_CUSTOMER_CANCELLED, source_space=classifier.config.source_space
        )
    )
    assert result.source_undocumented is False
