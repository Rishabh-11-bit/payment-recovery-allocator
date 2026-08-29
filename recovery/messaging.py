"""C10-content -- the customer-facing text a recovery contact would carry.

    python -m recovery.messaging --list

**Content only. Nothing here sends anything, ever, to anyone.**

Real SMS/WhatsApp delivery is on `NOT_BUILT.md`'s list, deliberately: TRAI's
DLT framework needs header and template registration that is not a same-week
process, and underneath that sits a question `DLT_COMPLIANCE.md` states
honestly rather than resolves -- is a payment-failure nudge promotional or
transactional? Different consent and registration rules follow depending on
the answer, and nobody has determined which applies.

That is a reason not to *dispatch*. It is not a reason to hide what the
customer would see if this were wired up -- a judge should be able to read
the actual sentence a contact carries, not just the word "RECOVERY_LINK" in
an audit log. This module generates that sentence and stops. Every
`MessageDraft` carries its own "not dispatched" note, and nothing in this
module imports anything that can make a network call.

## Deliberately not the C13 model

C13's `llama3` reads `error_description` and returns markers, never prose,
specifically because an unreviewed model has no business writing words a
customer will read about their own money. This module is templates, chosen
by `contact_kind` and refined by `cause_family`, both already decided
upstream -- nothing here classifies or decides, only renders. Every string a
person could plausibly send is authored, not generated, and stays that way
until someone reviews the wording, which is a different kind of judgment
than the taxonomy's and belongs to the same person.

## Deliberately English only

Hinglish and regional-language variants are a real, common feature of every
competing submission and are not built here. Machine-translating financial
copy without a fluent reviewer is exactly the kind of thing that produces a
technically-present feature nobody should trust; better to have one language
done honestly than several done by a model with no reviewer. Not on
`NOT_BUILT.md` because it is a template gap, not an architectural decision --
adding a language is copy work, not a design change.

## One correction this module makes, that the allocator does not

`card/business/international_transaction_not_allowed` and
`card/issuer_bank/payment_expired_card` are both TERMINAL/HIGH and both
currently resolve to the same action, `OFFER_RAIL_MIGRATION` /
`card_change_offer` -- see the flagged item in `CLAUDE.md` "Still open".
An expired card can be fixed by a card change. Razorpay's own captured
description for the other case says outright that the business accepts
domestic cards only, and "try another payment method" -- not "update your
card", which would be actively wrong. The *action* the allocator chose is
unchanged; the *wording* for that one cause family reflects what is actually
true rather than what the generic template for the action would say.
"""

from __future__ import annotations

import dataclasses

import typer

from allocator.decisions import CARD_CHANGE, DIFFERENT_CHANNEL, GENERIC_LINK
from recovery.models import Classification, ConfidenceBand, FailureClass, NormalizedFailure

app = typer.Typer(add_completion=False, help=__doc__)

NOT_DISPATCHED = (
    "CONTENT ONLY -- not sent by this system. See NOT_BUILT.md (real SMS/WhatsApp "
    "delivery) and DLT_COMPLIANCE.md (the open promotional/transactional question)."
)


@dataclasses.dataclass(frozen=True)
class MessageDraft:
    contact_kind: str
    cause_family: str | None
    subject: str
    body: str
    note: str = NOT_DISPATCHED


def _rupees(amount_paise: int) -> str:
    # "Rs", never the literal ₹ (U+20B9): every other module in this project
    # avoids it in console output because the Windows terminal codepoint this
    # project is developed against (cp1252) cannot encode it, and typer.echo
    # raises rather than degrading. Found by running this module's own CLI --
    # every other module already got this right; this one had not yet.
    return f"Rs {amount_paise / 100:,.2f}"


# Per-`contact_kind` base copy -- what a case gets when no cause_family
# refinement below applies. Every base template is deliberately non-specific
# about cause: it is what LOW-band cases get, where the class itself is a
# guess, so the wording must be right whichever cause turns out to be true.
_BASE: dict[str, tuple[str, str]] = {
    GENERIC_LINK: (
        "Your payment didn't go through",
        "Your payment of {amount} didn't complete. You can finish it here: {link}. "
        "If this was already resolved, no action is needed.",
    ),
    CARD_CHANGE: (
        "Update your payment method",
        "We couldn't charge your card for {amount}. Update your payment method to "
        "keep your subscription active: {link}",
    ),
    DIFFERENT_CHANNEL: (
        "Complete your payment",
        "Your payment of {amount} needs one more step. Try again here, on a "
        "different device or app if the first attempt didn't work: {link}",
    ),
}

# Refinements keyed on (contact_kind, cause_family). Only present where the
# cause genuinely changes what is true to say -- most cause families reuse the
# base template for their contact_kind unchanged, on purpose: a refinement
# that exists only to sound more specific, without changing what is claimed,
# would be decoration.
_REFINED: dict[tuple[str, str], tuple[str, str]] = {
    (CARD_CHANGE, "instrument"): (
        "Your card needs updating",
        "Your card couldn't be charged {amount} -- it may have expired or been "
        "blocked by your bank. Update it here to keep your subscription active: "
        "{link}",
    ),
    # The correction described in the module docstring. Informed by the
    # captured description for this exact reason code (see fixtures/payments):
    # "this business accepts domestic (Indian) card payments only. Try another
    # payment method." A card *change* is not promised to fix this -- an
    # Indian-issued card, or a different method entirely, might.
    (CARD_CHANGE, "merchant_configuration"): (
        "This card isn't accepted here",
        "Your payment of {amount} couldn't go through -- this business accepts "
        "domestic (Indian) cards only. Try an Indian-issued card or a different "
        "payment method: {link}",
    ),
    (GENERIC_LINK, "funding"): (
        "Your payment didn't go through",
        "Your payment of {amount} didn't complete -- it looks like a balance "
        "issue. The link below stays open, so you can complete it whenever suits: "
        "{link}",
    ),
    (GENERIC_LINK, "funds_committed"): (
        "Your payment didn't go through",
        "Your payment of {amount} didn't complete -- your balance may be held by "
        "another payment for now. This link stays open if you'd like to try again "
        "once that clears: {link}",
    ),
    (DIFFERENT_CHANNEL, "acknowledgement"): (
        "One step left on your payment",
        "Your payment of {amount} is still waiting on your confirmation. If you "
        "didn't see the request, you can confirm here instead: {link}",
    ),
    (GENERIC_LINK, "psp_capability"): (
        "Try a different UPI app",
        "Your payment of {amount} couldn't complete through that UPI app for this "
        "kind of payment. A different UPI app may work: {link}",
    ),
}


def draft_message(
    classification: Classification,
    contact_kind: str,
    *,
    amount_paise: int,
    link_url: str = "{recovery_link}",
) -> MessageDraft:
    """Render the message a `contact_kind` decision would carry.

    `link_url` defaults to a placeholder -- this module never has a real
    Payment Link to point at (that is `recovery/executor.py`'s job, on
    request only). Pass the real `short_url` from an `ExecutionResult` to
    render the actual sentence a customer would see.
    """
    key = (contact_kind, classification.cause_family or "")
    subject, body = _REFINED.get(key) or _BASE.get(
        contact_kind, _BASE[GENERIC_LINK]
    )
    return MessageDraft(
        contact_kind=contact_kind,
        cause_family=classification.cause_family,
        subject=subject,
        body=body.format(amount=_rupees(amount_paise), link=link_url),
    )


def _placeholder_classification(cause_family: str | None) -> Classification:
    """A minimal Classification carrying only what rendering needs: band and
    cause_family. The class/key values are inert -- draft_message keys
    entirely on contact_kind and cause_family, never on failure_class."""
    return Classification(
        failure_class=FailureClass.TERMINAL,
        confidence=0.9,
        band=ConfidenceBand.HIGH,
        key=NormalizedFailure(method=None, source=None, step=None, reason=None),
        mapped=True,
        cause_family=cause_family,
    )


@app.command()
def main() -> None:
    """Render every contact_kind x cause_family combination this module has a
    template for, so the actual wording is reviewable in one pass rather than
    read out of template source. Each `_REFINED` entry, plus each
    `contact_kind`'s default (what a LOW-band case gets, and what any
    cause_family without a specific refinement falls back to)."""
    typer.echo(f"  {NOT_DISPATCHED}\n")

    for contact_kind, (subject, _body) in _BASE.items():
        draft = draft_message(
            _placeholder_classification(None), contact_kind, amount_paise=49900
        )
        typer.echo(f"    [{contact_kind} / default]")
        typer.echo(f"      {draft.subject}")
        typer.echo(f"      {draft.body}\n")

    for contact_kind, family in _REFINED:
        draft = draft_message(
            _placeholder_classification(family), contact_kind, amount_paise=49900
        )
        typer.echo(f"    [{contact_kind} / {family}]")
        typer.echo(f"      {draft.subject}")
        typer.echo(f"      {draft.body}\n")


if __name__ == "__main__":
    app()
