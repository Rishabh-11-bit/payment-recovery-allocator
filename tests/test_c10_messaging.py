"""C10-content -- the customer-facing text, and the fence around it.

The point of these tests is the same as C13's: this component must be
provably inert on the money decision, and must never claim to have sent
anything. Two more things are worth pinning here specifically -- that the
project's established "Rs" convention is followed, because it broke once
already while this module was being written, and that the merchant-
configuration correction actually says something different from the
instrument-expired case rather than merely existing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from allocator.decisions import CARD_CHANGE, DIFFERENT_CHANNEL, GENERIC_LINK
from recovery.messaging import (
    NOT_DISPATCHED,
    MessageDraft,
    _placeholder_classification,
    _rupees,
    draft_message,
)


def classification(cause_family: str | None = None):
    return _placeholder_classification(cause_family)


# --------------------------------------------------------- the fence --- #


def test_every_draft_carries_the_not_dispatched_note():
    draft = draft_message(classification(), GENERIC_LINK, amount_paise=49900)
    assert draft.note == NOT_DISPATCHED
    assert "not sent" in draft.note.lower() or "not dispatched" in draft.note.lower()


def test_messaging_module_cannot_reach_the_network():
    """No import of urllib, requests, or the executor -- content generation
    must be structurally incapable of dispatch, not merely undocumented as
    doing it."""
    source = pathlib.Path("recovery/messaging.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"urllib", "requests", "httpx", "recovery.executor"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {node.module} if node.module else set()
        else:
            continue
        assert not (names & forbidden), f"messaging.py imports {names & forbidden}"


def test_messaging_module_does_not_import_the_c13_model():
    """Templates are authored, not generated -- see the module docstring's
    'deliberately not the C13 model' section."""
    source = pathlib.Path("recovery/messaging.py").read_text(encoding="utf-8")
    assert "recovery.enrich" not in source
    assert "import enrich" not in source


# ------------------------------------------------------- the Rs bug --- #


def test_no_draft_contains_the_literal_rupee_symbol():
    """Every other module in this project spells out 'Rs' rather than the
    literal ₹ (U+20B9), because typer.echo raises UnicodeEncodeError on the
    Windows console codepage this project develops against rather than
    degrading. This module introduced the one exception while being written
    -- caught by running its own CLI, not by review -- and this pins it so
    the same mistake cannot silently return."""
    for contact_kind in (GENERIC_LINK, CARD_CHANGE, DIFFERENT_CHANNEL):
        draft = draft_message(classification(), contact_kind, amount_paise=49900)
        assert "₹" not in draft.subject
        assert "₹" not in draft.body


def test_rupees_matches_the_project_wide_convention():
    assert _rupees(49900) == "Rs 499.00"
    assert _rupees(100000) == "Rs 1,000.00"


# ------------------------------------------- the merchant-config correction --- #


def test_expired_card_and_merchant_configuration_get_different_wording():
    """The finding in CLAUDE.md 'Still open': both are TERMINAL/HIGH and both
    currently resolve to card_change_offer as an *action*. The wording must
    not claim the same remedy for both -- an expired card can be fixed by a
    card change; the documented merchant-configuration case cannot."""
    instrument = draft_message(
        classification("instrument"), CARD_CHANGE, amount_paise=49900
    )
    merchant_config = draft_message(
        classification("merchant_configuration"), CARD_CHANGE, amount_paise=49900
    )
    assert instrument.body != merchant_config.body
    assert "expired" in instrument.body.lower() or "blocked" in instrument.body.lower()


def test_merchant_configuration_wording_does_not_promise_a_card_change_fixes_it():
    """The specific dishonesty this correction exists to avoid: telling a
    customer to 'update your card' when the documented cause is that the
    business does not accept international cards at all -- a different card
    of the same nationality would fail identically."""
    draft = draft_message(
        classification("merchant_configuration"), CARD_CHANGE, amount_paise=49900
    )
    assert "update your card" not in draft.body.lower()
    assert "domestic" in draft.body.lower() or "indian" in draft.body.lower()


# ---------------------------------------------------------- fallback --- #


def test_unrecognised_cause_family_falls_back_to_the_base_template():
    """A cause_family with no specific refinement must still render something
    -- silently producing no draft would be worse than a generic one."""
    draft = draft_message(
        classification("some_new_cause_family_not_yet_templated"),
        GENERIC_LINK,
        amount_paise=49900,
    )
    base = draft_message(classification(None), GENERIC_LINK, amount_paise=49900)
    assert draft.body == base.body


def test_unknown_contact_kind_does_not_crash():
    draft = draft_message(classification(), "some_future_contact_kind", amount_paise=49900)
    assert isinstance(draft, MessageDraft)


# ---------------------------------------------------------- rendering --- #


def test_amount_and_link_are_both_interpolated():
    draft = draft_message(
        classification(), GENERIC_LINK, amount_paise=123456, link_url="https://rzp.io/abc"
    )
    assert "Rs 1,234.56" in draft.body
    assert "https://rzp.io/abc" in draft.body
    assert "{amount}" not in draft.body and "{link}" not in draft.body
