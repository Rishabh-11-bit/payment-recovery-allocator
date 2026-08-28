"""C10 -- rail actions: shaping the checkout on a recovery Payment Link.

**This is the out-of-session case, and that boundary is the whole point.**

Razorpay's Optimizer already does in-session fallback routing: the customer is
on the page, a gateway fails, and traffic moves. Nothing here competes with
that. This is the link sent *afterwards*, to a customer who has already gone --
where there is no session to fall back within, and the only lever is what the
checkout offers when they come back.

Optimizer picks *which gateway*. This picks *what the customer is shown*, given
why the last attempt failed. Different axis, different moment.

## Two levers, and the gate between them

`options.checkout` supports coarse method on/off, and
`config.display.blocks` for instrument-level control -- removing a specific
bank, restricting by issuer -- plus `sequence` for ordering and
`preferences.show_default_blocks: false` to build an allowlist.

* **Reorder** promotes the likely rail and removes nothing. Always available.
* **Exclude** removes the degraded instrument. **HIGH band only.**

The gate is not caution for its own sake. Excluding on a misdiagnosis makes
recovery *harder* -- the customer is left without the method they would have
used, on a page they already abandoned once. Reorder costs nothing if wrong;
exclusion costs the recovery. So reorder is the default and exclusion needs the
band that says we know which instrument is degraded.

## What this module does not do

It builds payloads. It does not decide -- the allocator already chose the
shaping and the band already gated it, and re-deciding here would put the same
judgement in two places that could disagree.

It also does not execute a mandate migration. `OFFER_RAIL_MIGRATION` is
customer-mediated: the system offers, the customer acts. What is built here is
the *offer*, validated against the documented migration graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from recovery.models import Classification, DecisionAction, FailureClass

# Documented migration graph. UPI and Emandate can only move to Card; Card is
# the hub. Every edge is an offer the customer acts on, never a switch.
MIGRATION_GRAPH: Mapping[str, frozenset[str]] = {
    "card": frozenset({"card", "upi", "emandate"}),
    "upi": frozenset({"card"}),
    "emandate": frozenset({"card"}),
}

# Which method a failure class points away from, when we are confident enough
# to point. Content, not machinery -- but it is a mapping from class to
# *instrument behaviour*, not to a recovery probability, so it stays ordinal.
DEGRADED_METHOD = {
    FailureClass.INFRASTRUCTURE: None,  # the rail is fine; the incident was ours
    FailureClass.LIQUIDITY: None,  # the instrument works, the balance does not
    FailureClass.ATTENTION: None,  # they were reached; nothing is degraded
    FailureClass.TERMINAL: "the failed instrument",
}


class RailActionError(ValueError):
    pass


@dataclass(frozen=True)
class CheckoutShaping:
    """A built `options.checkout` payload, plus why it looks the way it does."""

    options: Mapping[str, Any]
    action: DecisionAction
    rationale: str
    excluded: tuple[str, ...] = ()
    promoted: tuple[str, ...] = ()

    @property
    def removes_anything(self) -> bool:
        return bool(self.excluded)

    def as_payment_link_payload(self, reference_id: str) -> dict[str, Any]:
        """The Payment Link body.

        `reference_id` links back to the original order. Recovery links carry it
        so a late authorisation on the original chain can still be reconciled
        against this one -- the Orders API clubs attempts against one order, and
        that is the mechanism the safety invariant relies on.
        """
        return {"reference_id": reference_id, "options": {"checkout": dict(self.options)}}


def _blocks_for(preferred: Sequence[str], excluded: Sequence[str]) -> dict[str, Any]:
    """`config.display.blocks` with `sequence` for ordering.

    Order is expressed by `sequence`, not by dict order: dict order is not a
    documented contract and a payload that depends on it is a payload that
    breaks silently when serialised somewhere else.
    """
    blocks: dict[str, Any] = {}
    for index, method in enumerate(preferred):
        blocks[f"promoted_{method}"] = {
            "name": f"Pay by {method.upper()}",
            "instruments": [{"method": method}],
        }
    display: dict[str, Any] = {
        "blocks": blocks,
        "sequence": [f"promoted_{method}" for method in preferred],
    }
    if excluded:
        # An allowlist: hide the defaults, show only what we listed. This is the
        # only construction that actually removes an instrument -- omitting it
        # from `blocks` while defaults are shown does nothing.
        display["preferences"] = {"show_default_blocks": False}
    return display


def build_shaping(
    classification: Classification,
    rail: str,
    *,
    preferred_order: Sequence[str] | None = None,
) -> CheckoutShaping:
    """Turn a classification into a checkout payload.

    The band decides which lever is available; the class decides what to point
    at. Neither is re-derived here.
    """
    if rail not in MIGRATION_GRAPH:
        raise RailActionError(f"unknown rail {rail!r}")

    # Promote the alternatives the graph actually permits, most-likely first.
    targets = [t for t in sorted(MIGRATION_GRAPH[rail]) if t != rail]
    order = list(preferred_order) if preferred_order else targets + [rail]

    may_exclude = classification.may_exclude_instrument
    degraded = DEGRADED_METHOD.get(classification.failure_class)

    if may_exclude and degraded is not None:
        excluded = (rail,)
        promoted = tuple(m for m in order if m != rail)
        options = {
            "config": {"display": _blocks_for(promoted, excluded)},
            "method": {rail: False},
        }
        return CheckoutShaping(
            options=options,
            action=DecisionAction.EXCLUDE_INSTRUMENT,
            rationale=(
                f"{classification.failure_class.value} at HIGH confidence: the "
                f"{rail} instrument is degraded and is removed, not merely demoted"
            ),
            excluded=excluded,
            promoted=promoted,
        )

    promoted = tuple(order)
    return CheckoutShaping(
        options={"config": {"display": _blocks_for(promoted, ())}},
        action=DecisionAction.REORDER_RAILS,
        rationale=(
            f"{classification.failure_class.value} at {classification.band.value}: "
            "promote without removing -- excluding on a misdiagnosis makes recovery harder"
        ),
        promoted=promoted,
    )


def migration_offer(rail: str, target: str) -> dict[str, Any]:
    """An `OFFER_RAIL_MIGRATION` payload, validated against the graph.

    The system offers; the customer acts. Manual charging of a domestic card is
    not supported, so there is no version of this that executes.
    """
    allowed = MIGRATION_GRAPH.get(rail)
    if allowed is None:
        raise RailActionError(f"unknown rail {rail!r}")
    if target not in allowed or target == rail:
        raise RailActionError(
            f"{rail} cannot migrate to {target}: the documented graph permits "
            f"{sorted(allowed - {rail})}"
        )
    return {
        "action": DecisionAction.OFFER_RAIL_MIGRATION.value,
        "from_rail": rail,
        "to_rail": target,
        "executed_by": "customer",
        "mechanism": "hosted page / card change",
    }


def migration_targets(rail: str) -> frozenset[str]:
    allowed = MIGRATION_GRAPH.get(rail)
    if allowed is None:
        raise RailActionError(f"unknown rail {rail!r}")
    return allowed - {rail}
