"""Normalization: raw entity -> (method, source, step, reason).

Extraction and value-space validation only. Nothing here decides what a key
means.
"""

from __future__ import annotations

from datetime import datetime, timezone

from recovery.models import PaymentStatus, PaymentSnapshot
from recovery.normalize import normalize_entity, normalize_snapshot

SPACE = {
    "card": frozenset({"customer", "business", "internal", "gateway", "issuer_bank"}),
    "upi": frozenset(
        {
            "customer",
            "business",
            "internal",
            "gateway",
            "issuer_bank",
            "customer_psp",
            "network",
            "beneficiary_bank",
        }
    ),
    "emandate": frozenset(
        {"customer", "bank", "business", "internal", "gateway", "issuer_bank"}
    ),
}


def entity(**overrides):
    base = {
        "method": "upi",
        "error_source": "customer_psp",
        "error_step": "payment_debit_response",
        "error_reason": "insufficient_funds",
    }
    base.update(overrides)
    return base


def test_extracts_the_four_part_key():
    key = normalize_entity(entity())
    assert key.key == ("upi", "customer_psp", "payment_debit_response", "insufficient_funds")


def test_values_are_lowercased_and_stripped():
    key = normalize_entity(entity(method="  UPI  ", error_source="Customer_PSP"))
    assert key.method == "upi"
    assert key.source == "customer_psp"


def test_missing_components_are_recorded_not_invented():
    key = normalize_entity(entity(error_step=None, error_reason=None))
    assert key.step is None
    assert key.reason is None
    assert key.missing == ("reason", "step")


def test_empty_string_counts_as_missing():
    key = normalize_entity(entity(error_reason="   "))
    assert key.reason is None
    assert "reason" in key.missing


def test_describe_is_readable_for_the_audit_trail():
    assert normalize_entity(entity(error_reason=None)).describe() == (
        "upi/customer_psp/payment_debit_response/-"
    )


# ----------------------------------------- method-partitioned value space -- #


def test_source_valid_for_its_own_method():
    key = normalize_entity(entity(), source_space=SPACE)
    assert key.source_in_documented_space is True


def test_source_from_another_method_is_flagged():
    """`customer_psp` is UPI-only. On a card it is anomalous, not a card source."""
    key = normalize_entity(
        entity(method="card", error_source="customer_psp"), source_space=SPACE
    )
    assert key.source_in_documented_space is False


def test_bare_bank_is_documented_for_emandate_and_netbanking():
    """Netbanking `bank` is here on the evidence of a real capture."""
    assert normalize_entity(
        entity(method="emandate", error_source="bank"), source_space=SPACE
    ).source_in_documented_space

    assert not normalize_entity(
        entity(method="card", error_source="bank"), source_space=SPACE
    ).source_in_documented_space


def test_razorpay_is_not_a_source_anywhere():
    for method in SPACE:
        key = normalize_entity(
            entity(method=method, error_source="razorpay"), source_space=SPACE
        )
        assert key.source_in_documented_space is False


def test_unknown_method_does_not_fail_validation():
    """No value space for it, so the key simply will not match and falls back."""
    key = normalize_entity(entity(method="wallet"), source_space=SPACE)
    assert key.source_in_documented_space is True


def test_validation_is_skipped_when_no_space_is_supplied():
    assert normalize_entity(entity(error_source="anything")).source_in_documented_space


# ------------------------------------------------------------- aliases ---- #


def test_aliases_are_not_applied_by_default():
    key = normalize_entity(entity(error_source="razorpay"), source_space=SPACE)
    assert key.source == "razorpay", "no silent rewriting"
    assert key.aliases_applied == ()


def test_alias_application_is_recorded():
    key = normalize_entity(
        entity(error_source="razorpay"),
        source_space=SPACE,
        source_aliases={"razorpay": "internal"},
    )
    assert key.source == "internal"
    assert key.aliases_applied == (("razorpay", "internal"),)
    assert key.source_in_documented_space is True, "validated after aliasing"


# ------------------------------------------------------------ snapshot ---- #


def test_normalizes_from_authoritative_state():
    snapshot = PaymentSnapshot(
        id="pay_1",
        status=PaymentStatus.FAILED,
        method="card",
        error_source="issuer_bank",
        error_step="payment_authorization",
        error_reason="payment_expired_card",
        fetched_at=datetime.now(timezone.utc),
    )
    key = normalize_snapshot(snapshot, source_space=SPACE)

    assert key.key == ("card", "issuer_bank", "payment_authorization", "payment_expired_card")
    assert key.source_in_documented_space is True
