"""The authored mandate-registration, funds-committed and PSP-capability rows.

Two things are pinned here. The **outcomes** -- every row landing on the class,
confidence, band and cause family it was authored with -- and the **precedence**
that makes them reachable at all, because three of these rows were shadowed by
broader rules the first time they were added and the shadowing was silent.
"""

from __future__ import annotations

import pytest
import yaml

from recovery.classifier import (
    Classifier,
    ClassifierConfigError,
    load_classifier,
)
from recovery.models import ConfidenceBand, FailureClass, NormalizedFailure

DEFAULT = "config/classifier.yaml"


@pytest.fixture(scope="module")
def classifier() -> Classifier:
    return load_classifier(DEFAULT)


def key(method: str, source: str, step: str, reason: str) -> NormalizedFailure:
    return NormalizedFailure(method=method, source=source, step=step, reason=reason)


# ------------------------------------------------------------- the rows --- #

# (method, source, step, reason, class, confidence, band, cause_family)
#
# The source and step columns are chosen to be *hostile*: each one is a context
# where a broader rule already exists, so a row that only passes because nothing
# else matched would fail here.
AUTHORED = [
    ("upi", "bank", "mandate_creation", "mandate_creation_declined",
     FailureClass.TERMINAL, 0.85, ConfidenceBand.HIGH, "mandate_registration"),
    ("upi", "bank", "mandate_creation", "mandate_creation_expired",
     FailureClass.ATTENTION, 0.80, ConfidenceBand.MODERATE, "mandate_registration"),
    ("upi", "bank", "mandate_creation", "mandate_creation_timeout",
     FailureClass.INFRASTRUCTURE, 0.75, ConfidenceBand.MODERATE, "mandate_registration"),
    ("upi", "customer_psp", "payment_debit_response", "funds_blocked_by_mandate",
     FailureClass.LIQUIDITY, 0.88, ConfidenceBand.HIGH, "funds_committed"),
    ("emandate", "bank", "payment_debit_response", "insufficient_funds_mandate_block",
     FailureClass.LIQUIDITY, 0.88, ConfidenceBand.HIGH, "funds_committed"),
    ("upi", "customer_psp", "payment_initiation", "upi_autopay_not_supported_on_psp",
     FailureClass.TERMINAL, 0.95, ConfidenceBand.HIGH, "psp_capability"),
    ("upi", "customer", "payment_authentication", "reqauth_mandate_not_acknowledged",
     FailureClass.ATTENTION, 0.70, ConfidenceBand.MODERATE, "acknowledgement"),
]


@pytest.mark.parametrize(
    "method, source, step, reason, expected, confidence, band, family",
    AUTHORED,
    ids=[row[3] for row in AUTHORED],
)
def test_authored_row_classifies_as_written(
    classifier, method, source, step, reason, expected, confidence, band, family
):
    result = classifier.classify(key(method, source, step, reason))
    assert result.mapped
    assert result.failure_class is expected
    assert result.confidence == pytest.approx(confidence)
    assert result.band is band
    assert result.cause_family == family


def test_the_generic_registration_row_lands_in_the_low_row(classifier):
    """0.50 and deliberately low, so the cost matrix decides, not the class."""
    result = classifier.classify(key("upi", "bank", "mandate_creation", "mandate_creation_failed"))
    assert result.band is ConfidenceBand.LOW
    assert result.deliberately_low_confidence
    # The intended reading survives on the record even though it does not act.
    assert result.cost_resolved_from is FailureClass.INFRASTRUCTURE


def test_psp_capability_is_terminal_for_a_reason_no_other_terminal_row_shares(classifier):
    """It is a property of the app, not the instrument, mandate or account.

    The row is TERMINAL and sits at `payment_initiation`, where a broader UPI
    rule says INFRASTRUCTURE. That is the expensive direction to get wrong --
    TERMINAL misread as INFRASTRUCTURE costs 10 in the matrix -- so it is worth
    a test of its own rather than a line in the table above.
    """
    psp = classifier.classify(key("upi", "customer_psp", "payment_initiation",
                                  "upi_autopay_not_supported_on_psp"))
    generic = classifier.classify(key("upi", "gateway", "payment_initiation",
                                      "gateway_technical_error"))
    assert psp.failure_class is FailureClass.TERMINAL
    assert generic.failure_class is FailureClass.INFRASTRUCTURE


def test_funds_committed_is_a_separate_family_from_funding(classifier):
    """Both LIQUIDITY. A held balance has a release date; an empty one does not."""
    committed = classifier.classify(
        key("upi", "customer_psp", "payment_debit_response", "funds_blocked_by_mandate")
    )
    funding = classifier.classify(
        key("card", "issuer_bank", "payment_authorization", "insufficient_funds")
    )
    assert committed.failure_class is funding.failure_class is FailureClass.LIQUIDITY
    assert committed.cause_family != funding.cause_family


# ----------------------------------------------------------- precedence --- #


def test_reason_outranks_a_broader_rule_naming_more_fields(classifier):
    """The bug these rows exposed: field count is not informativeness.

    `{method: upi, step: payment_initiation}` names two fields;
    `{reason: upi_autopay_not_supported_on_psp}` names one. Counting fields the
    second loses, the row never fires, and nothing says so -- it is present,
    valid, and dead. Naming the *cause* is the more specific claim.
    """
    rules = {rule.index: rule for rule in classifier.config.rules}
    reason_only = next(
        r for r in rules.values()
        if r.match == {"reason": "upi_autopay_not_supported_on_psp"}
    )
    broader = next(
        r for r in rules.values()
        if r.match == {"method": "upi", "step": "payment_initiation"}
    )
    assert reason_only.specificity < broader.specificity
    assert reason_only.precedence > broader.precedence


def test_naming_more_of_the_rest_still_wins_among_equals(classifier):
    """Precedence is lexicographic, so count breaks ties within a field tier."""
    rules = classifier.config.rules
    both = next(r for r in rules if r.match.get("reason") == "payment_expired_card")
    reason_only = next(
        r for r in rules if r.match == {"reason": "mandate_creation_declined"}
    )
    assert both.precedence > reason_only.precedence


# ---------------------------------------------------------- step space --- #


def test_the_two_documented_steps_with_no_row_are_recorded(classifier):
    """Recorded precisely because nothing matches on them yet."""
    for method in ("card", "upi", "netbanking", "emandate"):
        assert "customer_onboarding" in classifier.config.step_space[method]
        assert "account_management" in classifier.config.step_space[method]

    matched_steps = {r.match["step"] for r in classifier.config.rules if "step" in r.match}
    assert "customer_onboarding" not in matched_steps
    assert "account_management" not in matched_steps


def test_every_step_a_rule_names_is_in_the_step_space(classifier):
    union = frozenset().union(*classifier.config.step_space.values())
    for rule in classifier.config.rules:
        if "step" in rule.match:
            assert rule.match["step"] in union, rule.match


def test_a_mistyped_step_is_rejected_at_load(tmp_path):
    """The failure this check exists for is silent, not loud.

    A rule naming a step that does not exist matches nothing. It stays in the
    file, counts toward the rule total, and reads as coverage. Loading has to
    be where that dies, because nothing downstream can tell a dead rule from
    one whose key simply never arrived.
    """
    raw = yaml.safe_load(open(DEFAULT, encoding="utf-8").read())
    raw["rules"].append(
        {
            "match": {"method": "upi", "step": "payment_authentification"},
            "class": "ATTENTION",
            "confidence": 0.9,
        }
    )
    path = tmp_path / "typo.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ClassifierConfigError, match="not in the step space"):
        load_classifier(path)


def test_a_payload_step_outside_the_space_still_classifies(classifier):
    """Strict for rules, lower-bound for payloads. CHALLENGES 007."""
    result = classifier.classify(
        key("upi", "customer_psp", "a_step_no_reference_lists", "insufficient_funds")
    )
    assert result is not None


# --------------------------------------------------------- unreachable --- #


def test_the_registration_rows_are_marked_unreachable(classifier):
    """`payment.failed` is not triggered on an authorisation failure."""
    unreachable = {r.match["reason"] for r in classifier.config.unreachable_rules}
    assert unreachable == {
        "mandate_creation_declined",
        "mandate_creation_expired",
        "mandate_creation_failed",
        "mandate_creation_timeout",
    }


def test_nothing_reachable_is_marked_unreachable(classifier):
    """A row that does fire must not claim it cannot -- the flag would rot."""
    for rule in classifier.config.unreachable_rules:
        assert rule.match.get("reason", "").startswith("mandate_creation")
