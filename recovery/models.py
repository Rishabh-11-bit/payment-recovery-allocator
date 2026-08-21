"""Pydantic models for C1.

Three layers, deliberately distinct:

* **Envelope** -- what Razorpay delivered over HTTP. Headers matter: the dedup
  key `x-razorpay-event-id` is a header, not a body field.
* **Snapshot** -- authoritative payment state, fetched from the API. This is what
  a decision is allowed to read. The envelope body is *evidence that something
  happened*, never the current state.
* **Case / Decision / AuditEvent** -- our own records.

Parsing of the Razorpay entity is permissive (`extra="allow"`) because fixtures
are still being captured. The raw store keeps the payload verbatim regardless,
so nothing is lost if a field is missing from the model.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Permissive(BaseModel):
    """For anything shaped by Razorpay rather than by us."""

    model_config = ConfigDict(extra="allow", frozen=True)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class PaymentStatus(str, enum.Enum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    FAILED = "failed"

    @property
    def is_resolved(self) -> bool:
        """True if the money question is settled and no recovery should be attempted."""
        return self in (PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.REFUNDED)


class CaseState(str, enum.Enum):
    OPEN = "open"
    DECIDED = "decided"
    CLOSED_PAYMENT_RESOLVED = "closed_payment_resolved"


class DecisionAction(str, enum.Enum):
    """The allocator's action space (C3). C1 only ever emits HOLD.

    Selection logic is C3 and is hand-authored. This enum is the type surface,
    not the policy. There is deliberately no ATTEMPT_NOW -- see the PDN
    constraint in CLAUDE.md.
    """

    SCHEDULE_AT = "SCHEDULE_AT"
    RECOVERY_LINK = "RECOVERY_LINK"
    OFFER_RAIL_MIGRATION = "OFFER_RAIL_MIGRATION"
    REORDER_RAILS = "REORDER_RAILS"
    EXCLUDE_INSTRUMENT = "EXCLUDE_INSTRUMENT"
    HOLD = "HOLD"
    SURRENDER = "SURRENDER"


class AuditEventType(str, enum.Enum):
    WEBHOOK_RECEIVED = "webhook.received"
    WEBHOOK_DUPLICATE_IGNORED = "webhook.duplicate_ignored"
    WEBHOOK_REJECTED_UNSUPPORTED = "webhook.rejected_unsupported"
    EVENT_STALE_IGNORED = "event.stale_ignored"
    CASE_OPENED = "case.opened"
    CASE_ATTACHED = "case.attached"
    CASE_CLOSED_PAYMENT_RESOLVED = "case.closed_payment_resolved"
    STATE_REFRESHED = "payment.state_refreshed"
    STATE_REFRESH_FAILED = "payment.state_refresh_failed"
    DECISION_RECORDED = "decision.recorded"
    DECISION_DUPLICATE_SUPPRESSED = "decision.duplicate_suppressed"


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #


class WebhookEnvelope(_Model):
    """One webhook delivery, as received.

    `event_id` comes from the `x-razorpay-event-id` header and is the dedup key:
    delivery is at-least-once and duplicates are expected behaviour, not a fault.
    """

    event_id: Annotated[str, Field(min_length=1)]
    event: Annotated[str, Field(min_length=1)]
    created_at: int
    headers: dict[str, str]
    body: dict[str, Any]
    signature: str | None = None

    @classmethod
    def from_delivery(cls, headers: dict[str, str], body: dict[str, Any]) -> WebhookEnvelope:
        # Header names are case-insensitive on the wire.
        lowered = {k.lower(): v for k, v in headers.items()}
        event_id = lowered.get("x-razorpay-event-id")
        if not event_id:
            raise ValueError("delivery has no x-razorpay-event-id header; cannot dedupe")
        return cls(
            event_id=event_id,
            event=body.get("event", ""),
            created_at=int(body.get("created_at", 0)),
            headers=lowered,
            body=body,
            signature=lowered.get("x-razorpay-signature"),
        )

    @property
    def payment_entity(self) -> dict[str, Any]:
        return self.body.get("payload", {}).get("payment", {}).get("entity", {}) or {}


class PaymentSnapshot(_Permissive):
    """Authoritative payment state. Only ever built from an API fetch.

    Never construct this from a webhook body. The late-authorisation window means
    a payload saying `failed` can be describing a payment that is now authorized.
    """

    id: str
    status: PaymentStatus
    order_id: str | None = None
    amount: int | None = None
    currency: str | None = None
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# Our records
# --------------------------------------------------------------------------- #


class Case(_Model):
    """A recovery case: one attempt chain.

    Keyed on `order_id`, not `payment_id`. The safety invariant is defined over
    the *order's* attempt chain -- multiple failed payments against one order are
    one chain, and the Orders API clubs them. Keying on payment_id would open a
    fresh case per attempt and lose exactly the grouping the invariant needs.

    Payments with no order fall back to a payment-scoped key; that is audited
    rather than silently accepted, because for mandate debits it should not occur.
    """

    case_id: str
    chain_key: str
    order_id: str | None
    payment_id: str
    state: CaseState
    opened_at: datetime
    # Highest Razorpay `created_at` applied to this case. Guards against
    # out-of-order delivery regressing state.
    last_event_created_at: int


class Decision(_Model):
    """A decision taken on a case.

    `idempotency_key` is `recovery:{payment_id}:{policy_version}:{attempt_n}` and
    carries a uniqueness constraint in the store. Exactly-once is enforced there,
    not by checking-then-writing in application code.
    """

    idempotency_key: str
    case_id: str
    payment_id: str
    policy_version: str
    attempt_n: int
    action: DecisionAction
    reason: str
    decided_at: datetime


class AuditEvent(_Model):
    """Append-only. Never updated, never deleted -- enforced by trigger in store.py."""

    seq: int | None = None
    at: datetime
    case_id: str | None
    event_id: str | None
    event_type: AuditEventType
    detail: dict[str, Any] = Field(default_factory=dict)
