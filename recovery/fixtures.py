"""Deliveries built from captured payload shapes.

The entity shape here is taken from **real test-mode captures** in
`tests/fixtures/payments/`, not guessed. Differences the guess got wrong, worth
knowing because each would have surfaced at integration:

* `notes` comes back as an empty **list**, not an empty dict
* `acquirer_data`, `bank`, `vpa`, `wallet`, `card_id`, `international`,
  `amount_captured`, `amount_refunded`, `fee`, `tax`, `refund_status` and
  `invoice_id` are all present, mostly null
* `created_at` is a unix int on the entity as well as on the event envelope

The captured payments are also the source of the `CAPTURED_*` keys below, which
are real `(method, source, step, reason)` tuples rather than plausible ones.

**Still guessed:** the webhook *envelope* around the entity. Test mode produced
payment entities via the API; capturing an envelope needs a reachable endpoint,
so the `payload`/`event`/`created_at` wrapper here is documentation-derived.
`load_captured_deliveries()` reads real envelopes once they exist.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

FIXTURE_DIR = pathlib.Path("tests/fixtures")
PAYMENTS_DIR = FIXTURE_DIR / "payments"

# Real keys from captured test-mode failures. Use these in tests rather than
# inventing tuples -- an invented key can be internally consistent and still
# describe a payload the API never emits.
CAPTURED_GENERIC_DECLINE = {
    "method": "netbanking",
    "error_source": "bank",
    "error_step": "payment_authorization",
    "error_reason": "payment_failed",
    "error_description": (
        "Your payment didn't go through as it was declined by the bank. "
        "Try another payment method or contact your bank."
    ),
}
"""The canonical low-confidence case.

`source: bank` on netbanking is undocumented -- the reference lists a bare
`bank` only for emandate -- and the reason carries no information at all.
Razorpay's own description says only "try another payment method or contact your
bank". Any classifier that reports high confidence on this key is lying.
"""

CAPTURED_MERCHANT_CONFIG_TERMINAL = {
    "method": "card",
    "error_source": "business",
    "error_step": "payment_initiation",
    "error_reason": "international_transaction_not_allowed",
    "error_description": (
        "Your payment could not be completed as this business accepts domestic "
        "(Indian) card payments only. Try another payment method."
    ),
}
"""TERMINAL for a merchant-configuration reason rather than an instrument one.

The card is valid and will never work with this business. Same zero retry
probability as an expired card, but a card-change offer cannot fix it -- only
the merchant can. Hence `cause_family`.
"""

CAPTURED_CUSTOMER_CANCELLED = {
    "method": "wallet",
    "error_source": "customer",
    "error_step": "payment_authentication",
    "error_reason": "payment_cancelled",
    "error_description": "Your payment has been cancelled. Try again or complete the payment later.",
}
"""`wallet` is a method the source-space table does not cover at all."""


def build_entity(
    *,
    payment_id: str = "pay_SYNTH0000000001",
    order_id: str | None = "order_SYNTH000000001",
    status: str = "failed",
    amount: int = 100000,
    created_at: int = 1_787_383_699,
    method: str = "upi",
    error_code: str = "BAD_REQUEST_ERROR",
    error_source: str = "customer_psp",
    error_step: str = "payment_debit_response",
    error_reason: str = "insufficient_funds",
    error_description: str = "Your payment could not be completed.",
    **overrides: Any,
) -> dict[str, Any]:
    """A payment entity in the captured shape."""
    entity: dict[str, Any] = {
        "acquirer_data": {"bank_transaction_id": None},
        "amount": amount,
        "amount_captured": None,
        "amount_refunded": 0,
        "bank": None,
        "captured": False,
        "card_id": None,
        "contact": "+919999999999",
        "created_at": created_at,
        "currency": "INR",
        "description": f"#{payment_id[-14:]}",
        "email": "void@example.com",
        "entity": "payment",
        "error_code": error_code,
        "error_description": error_description,
        "error_reason": error_reason,
        "error_source": error_source,
        "error_step": error_step,
        "fee": None,
        "id": payment_id,
        "international": False,
        "invoice_id": None,
        "method": method,
        "notes": [],  # a list, not a dict -- this is what the API returns
        "order_id": order_id,
        "refund_status": None,
        "status": status,
        "tax": None,
        "vpa": None,
        "wallet": None,
    }
    entity.update(overrides)
    return entity


def build_delivery(
    *,
    event_id: str,
    payment_id: str = "pay_SYNTH0000000001",
    order_id: str | None = "order_SYNTH000000001",
    created_at: int = 1_755_000_000,
    status: str = "failed",
    method: str = "upi",
    error_source: str = "customer_psp",
    error_step: str = "payment_debit_response",
    error_reason: str = "insufficient_funds",
    amount: int = 49900,
    entity_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """One webhook delivery: (headers, body).

    `x-razorpay-event-id` is a header, matching the real delivery shape -- the
    dedup key does not live in the body.
    """
    headers = {
        "X-Razorpay-Event-Id": event_id,
        "X-Razorpay-Signature": "synthetic-not-verifiable",
        "Content-Type": "application/json",
    }
    entity = build_entity(
        payment_id=payment_id,
        order_id=order_id,
        status=status,
        amount=amount,
        created_at=created_at,
        method=method,
        error_source=error_source,
        error_step=error_step,
        error_reason=error_reason,
        **(entity_overrides or {}),
    )
    body = {
        "entity": "event",
        "account_id": "acc_SYNTH0000000001",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": created_at,
    }
    return headers, body


def load_captured_payments() -> list[dict[str, Any]]:
    """Real payment entities captured from test mode. Empty if none committed."""
    if not PAYMENTS_DIR.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(PAYMENTS_DIR.glob("*.json"))
    ]


def load_captured_deliveries() -> list[tuple[dict[str, str], dict[str, Any]]]:
    """Real captured webhook envelopes, if any have been committed yet.

    Returns an empty list until `tests/fixtures/webhooks/` is populated, which
    lets tests skip rather than fail while capture is still outstanding.
    """
    directory = FIXTURE_DIR / "webhooks"
    if not directory.is_dir():
        return []
    deliveries = []
    for path in sorted(directory.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        deliveries.append((record["headers"], record["body"]))
    return deliveries
