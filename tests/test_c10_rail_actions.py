"""C10 -- rail actions on a recovery Payment Link.

The gate is the thing under test: reorder is always available, exclusion needs
the HIGH band, and nothing here re-decides what the allocator already chose.
"""

from __future__ import annotations

import pytest

from recovery.contract import make_classification
from recovery.models import ConfidenceBand, DecisionAction, FailureClass
from recovery.rail_actions import (
    MIGRATION_GRAPH,
    RailActionError,
    build_shaping,
    migration_offer,
    migration_targets,
)


def shaped(classifier, failure_class, band, rail="upi"):
    return build_shaping(make_classification(classifier, failure_class, band), rail)


# --------------------------------------------------------------- the gate -- #


def test_exclusion_requires_the_high_band(classifier):
    high = shaped(classifier, FailureClass.TERMINAL, ConfidenceBand.HIGH)
    moderate = shaped(classifier, FailureClass.TERMINAL, ConfidenceBand.MODERATE)

    assert high.action is DecisionAction.EXCLUDE_INSTRUMENT
    assert moderate.action is DecisionAction.REORDER_RAILS


def test_reorder_removes_nothing(classifier):
    """Promoting is free if wrong. Removing is not."""
    shaping = shaped(classifier, FailureClass.TERMINAL, ConfidenceBand.MODERATE)
    assert not shaping.removes_anything
    assert "method" not in shaping.options
    assert "preferences" not in shaping.options["config"]["display"]


def test_low_band_never_excludes(classifier):
    for failure_class in FailureClass:
        shaping = shaped(classifier, failure_class, ConfidenceBand.LOW)
        assert shaping.action is DecisionAction.REORDER_RAILS
        assert not shaping.removes_anything


def test_high_band_on_a_working_instrument_still_only_reorders(classifier):
    """LIQUIDITY is an empty account, not a broken rail. Nothing to exclude."""
    shaping = shaped(classifier, FailureClass.LIQUIDITY, ConfidenceBand.HIGH)
    assert shaping.action is DecisionAction.REORDER_RAILS
    assert not shaping.removes_anything


def test_exclusion_actually_removes_the_instrument(classifier):
    """Omitting a block while defaults are shown removes nothing at all."""
    shaping = shaped(classifier, FailureClass.TERMINAL, ConfidenceBand.HIGH, rail="card")
    display = shaping.options["config"]["display"]

    assert display["preferences"]["show_default_blocks"] is False
    assert shaping.options["method"]["card"] is False
    assert "card" in shaping.excluded


# ------------------------------------------------------------- ordering --- #


def test_order_is_expressed_by_sequence_not_dict_order(classifier):
    """Dict order is not a documented contract; `sequence` is."""
    shaping = shaped(classifier, FailureClass.INFRASTRUCTURE, ConfidenceBand.HIGH)
    display = shaping.options["config"]["display"]
    assert display["sequence"]
    assert set(display["sequence"]) == set(display["blocks"])


def test_promoted_order_is_respected(classifier):
    shaping = build_shaping(
        make_classification(classifier, FailureClass.LIQUIDITY, ConfidenceBand.HIGH),
        "card",
        preferred_order=["upi", "card"],
    )
    assert shaping.promoted == ("upi", "card")
    assert shaping.options["config"]["display"]["sequence"] == [
        "promoted_upi",
        "promoted_card",
    ]


def test_unknown_rail_is_rejected(classifier):
    with pytest.raises(RailActionError, match="unknown rail"):
        shaped(classifier, FailureClass.TERMINAL, ConfidenceBand.HIGH, rail="wallet")


# ------------------------------------------------- migration is an offer -- #


def test_migration_validates_against_the_documented_graph():
    assert migration_targets("upi") == frozenset({"card"})
    assert migration_targets("emandate") == frozenset({"card"})
    assert migration_targets("card") == frozenset({"upi", "emandate"})


@pytest.mark.parametrize("rail, target", [("upi", "emandate"), ("emandate", "upi")])
def test_illegal_migrations_are_refused(rail, target):
    with pytest.raises(RailActionError, match="cannot migrate"):
        migration_offer(rail, target)


def test_upi_cannot_migrate_to_upi():
    with pytest.raises(RailActionError):
        migration_offer("upi", "upi")


def test_migration_payload_says_the_customer_executes_it():
    """Manual charging of a domestic card is not supported. There is no switch."""
    offer = migration_offer("upi", "card")
    assert offer["executed_by"] == "customer"
    assert offer["action"] == DecisionAction.OFFER_RAIL_MIGRATION.value


# ------------------------------------------------------------- payload ---- #


def test_link_payload_carries_the_reference_back_to_the_order(classifier):
    """The safety invariant relies on attempts clubbing against one order."""
    shaping = shaped(classifier, FailureClass.ATTENTION, ConfidenceBand.HIGH)
    payload = shaping.as_payment_link_payload("order_abc")

    assert payload["reference_id"] == "order_abc"
    assert payload["options"]["checkout"] == dict(shaping.options)


def test_shaping_explains_itself(classifier):
    for band in ConfidenceBand:
        shaping = shaped(classifier, FailureClass.TERMINAL, band)
        assert shaping.rationale
        assert band.value in shaping.rationale or "HIGH confidence" in shaping.rationale


def test_module_does_not_re_decide_the_band(classifier):
    """The allocator chose; this executes. Two places deciding can disagree."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("recovery/rail_actions.py").read_text(encoding="utf-8"))
    compared = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(side, ast.Attribute) and side.attr in {"confidence", "band"}
            for side in [node.left, *node.comparators]
        )
    ]
    assert not compared, "rail_actions re-derives the band instead of using the gate"
