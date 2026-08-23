"""Authoritative payment state.

This module exists because of one documented behaviour: a payment marked
`Failed` can still become `Authorized`. Razorpay polls the bank for ~3 days
after a timeout, and every T+1 / T+2 / T+3 retry lands inside that window.

So the webhook body is **evidence that something happened**, not a statement of
current state. Nothing in this system is permitted to decide from the payload
alone -- the worker re-fetches before every decision, and refuses to decide if
the fetch fails (`late_auth.require_state_refresh`).

Adapter pattern, mirroring Execute: `SimulatedGateway` is primary and is what
the simulator and tests run against. `RazorpayGateway` is demonstrated, not
depended on.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Protocol

from recovery.models import PaymentSnapshot, PaymentStatus


class StateRefreshError(RuntimeError):
    """The authoritative fetch failed. Never fall back to the webhook payload."""


class PaymentGateway(Protocol):
    def fetch_payment(self, payment_id: str) -> PaymentSnapshot: ...


def _snapshot_from_entity(entity: dict[str, Any]) -> PaymentSnapshot:
    return PaymentSnapshot(
        id=entity["id"],
        status=PaymentStatus(entity["status"]),
        order_id=entity.get("order_id"),
        amount=entity.get("amount"),
        currency=entity.get("currency"),
        method=entity.get("method"),
        error_code=entity.get("error_code"),
        error_description=entity.get("error_description"),
        error_source=entity.get("error_source"),
        error_step=entity.get("error_step"),
        error_reason=entity.get("error_reason"),
        order_expires_at=entity.get("order_expires_at"),
        fetched_at=datetime.now(timezone.utc),
    )


class SimulatedGateway:
    """In-memory gateway. Primary implementation for C5 and the tests.

    `set_state` is how a test expresses "the bank came back three days later":
    the stored entity is what a refresh will report, independently of whatever
    the webhook body said.
    """

    def __init__(self, entities: dict[str, dict[str, Any]] | None = None) -> None:
        self._entities: dict[str, dict[str, Any]] = dict(entities or {})
        self.fetch_count: int = 0
        self.fail_next: bool = False

    def seed_from_webhook(self, entity: dict[str, Any]) -> None:
        """Register a payment so a later refresh has something to return.

        Convenience for tests only. It does not make the webhook authoritative --
        the worker still fetches, and a test can overwrite the state first.
        """
        if entity.get("id"):
            self._entities.setdefault(entity["id"], dict(entity))

    def set_state(self, payment_id: str, **fields: Any) -> None:
        self._entities.setdefault(payment_id, {"id": payment_id})
        self._entities[payment_id].update(fields)

    def fetch_payment(self, payment_id: str) -> PaymentSnapshot:
        if self.fail_next:
            self.fail_next = False
            raise StateRefreshError(f"simulated refresh failure for {payment_id}")
        self.fetch_count += 1
        entity = self._entities.get(payment_id)
        if entity is None:
            raise StateRefreshError(f"unknown payment {payment_id}")
        return _snapshot_from_entity(entity)


class RazorpayGateway:
    """Demonstrated, not depended on. Stdlib only -- no SDK dependency added.

    Credentials come from the caller, which reads them from the environment.
    They are never logged, and never appear in an audit detail.
    """

    BASE = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, timeout: float = 10.0) -> None:
        if not key_id.startswith("rzp_test_"):
            raise ValueError("refusing to construct a gateway with a non-test key")
        self._token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self._timeout = timeout

    def fetch_payment(self, payment_id: str) -> PaymentSnapshot:
        request = urllib.request.Request(
            f"{self.BASE}/payments/{payment_id}",
            headers={"Authorization": f"Basic {self._token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                entity = json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            # Deliberately does not include the response body: it can echo the key id.
            raise StateRefreshError(f"fetch failed for {payment_id}: {type(exc).__name__}") from exc
        return _snapshot_from_entity(entity)
