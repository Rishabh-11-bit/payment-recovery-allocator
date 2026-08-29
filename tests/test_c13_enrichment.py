"""C13 — the only model in the project, and the fence around it.

The point of these tests is not that enrichment works. It is that enrichment
**cannot reach the money decision**, cannot make the system depend on a network,
and cannot fail loudly. Every one of those is asserted rather than intended,
because a component that is optional by design is exactly the kind that quietly
stops being optional.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from recovery.classifier import load_classifier
from recovery.enrich import (
    MARKERS,
    Observations,
    cache_key,
    call_model,
    observe,
    read_cache,
    refine,
    write_cache,
)
from recovery.models import (
    Classification,
    ConfidenceBand,
    FailureClass,
    NormalizedFailure,
)

DESCRIPTION = (
    "Your payment didn't go through as it was declined by the bank. "
    "Try another payment method or contact your bank."
)


@pytest.fixture(scope="module")
def families():
    return load_classifier().config.enrichment_families


def classification(
    band: ConfidenceBand = ConfidenceBand.LOW,
    *,
    cause_family: str | None = None,
    failure_class: FailureClass = FailureClass.TERMINAL,
) -> Classification:
    confidence = {
        ConfidenceBand.LOW: 0.30,
        ConfidenceBand.MODERATE: 0.70,
        ConfidenceBand.HIGH: 0.95,
    }[band]
    return Classification(
        failure_class=failure_class,
        confidence=confidence,
        band=band,
        key=NormalizedFailure(
            method="netbanking", source="bank", step="payment_authorization",
            reason="payment_failed",
        ),
        mapped=True,
        rule_index=7,
        cause_family=cause_family,
    )


def cached(tmp_path: pathlib.Path, markers: tuple[str, ...]) -> pathlib.Path:
    write_cache(DESCRIPTION, Observations(markers=markers), cache_dir=tmp_path)
    return tmp_path


# ------------------------------------------------------------ the fence --- #


def test_enrichment_changes_cause_family_and_nothing_else(tmp_path, families):
    """The load-bearing assertion. Every other field, compared explicitly.

    Not a spot-check of three fields -- the whole model is diffed, so a field
    added to `Classification` later is covered without anyone remembering to.
    """
    cached(tmp_path, ("instrument_unusable",))
    before = classification()
    after, _, family = refine(before, DESCRIPTION, families, cache_dir=tmp_path)

    assert family == "instrument"
    assert after.cause_family == "instrument"

    before_fields = before.model_dump()
    after_fields = after.model_dump()
    before_fields.pop("cause_family")
    after_fields.pop("cause_family")
    assert before_fields == after_fields, "enrichment touched a field other than cause_family"


@pytest.mark.parametrize("band", [ConfidenceBand.HIGH, ConfidenceBand.MODERATE])
def test_enrichment_cannot_touch_a_confident_classification(tmp_path, families, band):
    """Above LOW the enum key was informative. The text is not consulted."""
    cached(tmp_path, ("instrument_unusable",))
    before = classification(band)
    after, _, family = refine(before, DESCRIPTION, families, cache_dir=tmp_path)
    assert family is None
    assert after == before


def test_an_authored_cause_family_always_wins(tmp_path, families):
    """A family from the taxonomy outranks one extracted from prose."""
    cached(tmp_path, ("instrument_unusable",))
    before = classification(cause_family="generic_decline")
    after, _, family = refine(before, DESCRIPTION, families, cache_dir=tmp_path)
    assert family is None
    assert after.cause_family == "generic_decline"


def test_two_mapped_markers_is_ambiguity_not_a_choice(tmp_path, families):
    """Text supporting two families is the text being unclear.

    Picking one would be the model deciding, which is the line this component
    exists to not cross.
    """
    cached(tmp_path, ("instrument_unusable", "insufficient_balance"))
    before = classification()
    after, observations, family = refine(before, DESCRIPTION, families, cache_dir=tmp_path)
    assert len(observations.markers) == 2
    assert family is None
    assert after == before


def test_unmapped_markers_change_nothing(tmp_path, families):
    """`bank_referral` is the commonest phrase in these strings and means nothing."""
    cached(tmp_path, ("bank_referral",))
    before = classification()
    after, _, family = refine(before, DESCRIPTION, families, cache_dir=tmp_path)
    assert family is None
    assert after == before


def test_no_information_is_not_informative(tmp_path):
    cached(tmp_path, ("no_information",))
    assert not observe(DESCRIPTION, cache_dir=tmp_path).informative


# --------------------------------------------------- offline by default --- #


def test_no_cache_and_no_network_is_a_no_op(tmp_path, families):
    """A judge cloning this repo without a key must get the same classification."""
    before = classification()
    after, observations, family = refine(before, DESCRIPTION, families, cache_dir=tmp_path)
    assert observations.source == "unavailable"
    assert family is None
    assert after == before


def test_observe_never_reaches_the_network_unless_asked(tmp_path, monkeypatch):
    """`allow_network` defaults False, so no test or reproduce run can call out."""

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("network reached without allow_network=True")

    monkeypatch.setattr("recovery.enrich.call_model", explode)
    assert observe(DESCRIPTION, cache_dir=tmp_path).source == "unavailable"


def test_a_missing_api_key_is_unavailable_not_an_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert call_model(DESCRIPTION).source == "unavailable"


def test_a_corrupt_cache_entry_degrades_to_nothing(tmp_path, families):
    """Never a crash. A bad file on disk cannot break a classification."""
    path = tmp_path / f"{cache_key(DESCRIPTION)}.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    assert read_cache(DESCRIPTION, cache_dir=tmp_path) is None
    before = classification()
    after, _, _ = refine(before, DESCRIPTION, families, cache_dir=tmp_path)
    assert after == before


def test_markers_outside_the_vocabulary_are_dropped(tmp_path, families):
    """The model inventing a marker must not invent a cause family."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{cache_key(DESCRIPTION)}.json").write_text(
        json.dumps({"description": DESCRIPTION, "markers": ["definitely_terminal"]}),
        encoding="utf-8",
    )
    observations = read_cache(DESCRIPTION, cache_dir=tmp_path)
    assert observations.markers == ()
    after, _, family = refine(classification(), DESCRIPTION, families, cache_dir=tmp_path)
    assert family is None


@pytest.mark.parametrize("description", ["", "   ", None])
def test_empty_descriptions_are_handled(description, tmp_path, families):
    after, _, family = refine(classification(), description, families, cache_dir=tmp_path)
    assert family is None
    assert after == classification()


# ------------------------------------------------------------- caching --- #


def test_the_cache_key_is_versioned_by_prompt_and_model(monkeypatch):
    """A changed prompt must not silently reuse parses made under the old one."""
    original = cache_key(DESCRIPTION)
    monkeypatch.setattr("recovery.enrich.PROMPT_VERSION", "99")
    assert cache_key(DESCRIPTION) != original


def test_the_cache_round_trips(tmp_path):
    write_cache(DESCRIPTION, Observations(markers=("technical_fault",)), cache_dir=tmp_path)
    assert read_cache(DESCRIPTION, cache_dir=tmp_path).markers == ("technical_fault",)


def test_every_configured_marker_is_in_the_vocabulary(families):
    """A family keyed on a marker the model can never emit is dead config."""
    unknown = set(families) - set(MARKERS)
    assert not unknown, f"config maps markers the model cannot return: {sorted(unknown)}"


def test_bank_referral_is_deliberately_unmapped(families):
    """The commonest phrase in these strings, and it identifies nothing."""
    assert "bank_referral" not in families
    assert "no_information" not in families


# ---------------------------------------------------- the money boundary --- #


def test_no_allocator_module_imports_enrichment():
    """The fence, checked in the one place it could be breached.

    `allocator/` is where the money decision lives. If enrichment is ever
    imported there, the model is one edit away from selecting an action.
    """
    for path in pathlib.Path("allocator").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "recovery.enrich" not in source, f"{path} imports enrichment"
        assert "import enrich" not in source, f"{path} imports enrichment"


# ------------------------------------------- documented value spaces --- #


def test_every_documented_method_has_a_source_space():
    """A method absent from the map passes the documented-source check vacuously.

    That is what made two captured wallet payloads report
    `source_in_documented_space: True` while nothing had been checked -- absent
    from the map read exactly like validated. Wallet and Cardless EMI are in the
    same Razorpay reference as the other four, so the gap was an omission.
    """
    space = load_classifier().config.source_space
    for method in ("card", "upi", "netbanking", "emandate", "wallet", "cardless_emi"):
        assert method in space, f"{method} has no documented source space"


def test_the_captured_wallet_payload_is_checked_not_waved_through():
    from recovery.fixtures import load_captured_payments
    from recovery.normalize import normalize_entity

    config = load_classifier().config
    wallets = [p for p in load_captured_payments() if p.get("method") == "wallet"]
    assert wallets, "no captured wallet payload to check"
    for payment in wallets:
        key = normalize_entity(payment, source_space=config.source_space)
        assert key.source in config.source_space["wallet"]


def test_upi_step_space_covers_the_request_response_pairs():
    """`payment_debit_request` broke on the way to the bank; `_response` means
    the bank answered and declined. Different classes, and the space has to hold
    both before a rule can name either."""
    steps = load_classifier().config.step_space["upi"]
    for step in (
        "payment_debit_request",
        "payment_debit_response",
        "payment_credit_request",
        "payment_credit_response",
        "payment_status_request",
        "payment_status_response",
    ):
        assert step in steps, f"UPI step space is missing {step}"
