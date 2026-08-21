"""Synthetic deliveries — placeholder until real captures land.

**These are not calibration data and must not become fixtures of record.**
They exist so the event core can be built and proved before the test-mode
captures exist. The field *shapes* here are a guess; the real ones come from
`scripts/capture_fixtures.py`. See `tests/fixtures/README.md`.

When real captures land, the swap should be: read the JSON, keep the same
`build_delivery` signature. Nothing in C1 should need to change.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

FIXTURE_DIR = pathlib.Path("tests/fixtures")


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
    entity = {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": status,
        "order_id": order_id,
        "method": method,
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Your payment could not be completed.",
        "error_source": error_source,
        "error_step": error_step,
        "error_reason": error_reason,
    }
    body = {
        "entity": "event",
        "account_id": "acc_SYNTH0000000001",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": created_at,
    }
    return headers, body


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
