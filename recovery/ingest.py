"""Ingest: acknowledge fast, decide later.

Razorpay treats a response slower than 5s as a timeout and resends the event.
A resend is not free -- it costs another delivery and, without dedup, another
case. So this path does the least possible work: persist the payload verbatim,
enqueue, return. No parsing beyond what dedup needs, no state fetch, no
decision. All of that is the worker's job.

Dedup is on `x-razorpay-event-id`, a *header*. Delivery is at-least-once and
duplicates are expected behaviour rather than a fault, so a duplicate is
recorded in the audit trail and acknowledged 2xx -- never rejected. Rejecting
it would trigger Razorpay's backoff and, after 24h of non-2xx, disable the
webhook entirely.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from recovery.config import Config
from recovery.models import AuditEventType, WebhookEnvelope
from recovery.normalize import has_nested_error_object
from recovery.store import Store


class IngestOutcome(str, enum.Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNSUPPORTED = "unsupported"
    MALFORMED = "malformed"
    # A real delivery describing something that is not a failure.
    FILTERED = "filtered"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    event_id: str | None
    # Every outcome except MALFORMED acknowledges 2xx. A malformed delivery is
    # the one case where a non-2xx is right: it cannot be deduped, so accepting
    # it silently would be worse than making the failure visible.
    http_status: int

    @property
    def acknowledged(self) -> bool:
        return 200 <= self.http_status < 300


def ingest_delivery(
    store: Store,
    config: Config,
    headers: dict[str, str],
    body: dict[str, Any],
) -> IngestResult:
    try:
        envelope = WebhookEnvelope.from_delivery(headers, body)
    except (ValueError, TypeError) as exc:
        store.append_audit(
            AuditEventType.WEBHOOK_REJECTED_UNSUPPORTED,
            detail={"error": str(exc), "reason": "malformed_delivery"},
        )
        return IngestResult(IngestOutcome.MALFORMED, None, http_status=400)

    if envelope.event not in config.ingest.accepted_events:
        # Acknowledged: an event we do not handle is not an error, and a non-2xx
        # here would put the whole webhook into backoff.
        store.append_audit(
            AuditEventType.WEBHOOK_REJECTED_UNSUPPORTED,
            event_id=envelope.event_id,
            detail={"event": envelope.event, "accepted": list(config.ingest.accepted_events)},
        )
        return IngestResult(IngestOutcome.UNSUPPORTED, envelope.event_id, http_status=200)

    entity = envelope.payment_entity

    # A payload nesting its error fields would normalise to an empty key,
    # classify as unmapped and land in the LOW row -- safe and entirely silent.
    # Should never fire; audited loudly if it does. See CHALLENGES 014.
    if has_nested_error_object(entity):
        store.append_audit(
            AuditEventType.PAYLOAD_SHAPE_UNEXPECTED,
            event_id=envelope.event_id,
            detail={
                "expected": "flat error_code / error_source / error_step / error_reason",
                "found": "nested error object",
                "consequence": "every key would normalise empty; shape assumption is wrong",
            },
        )

    # Not every `payment.failed` is a failure. A mandate registration fires a
    # dummy debit to validate the mandate, and it is always `failed` -- filtered
    # here rather than downstream, so it never opens a case, never spends a
    # contact, and never enters the batch a result is computed over.
    reason = str(entity.get("error_reason") or "").strip().lower()
    if reason in config.ingest.filtered_reasons:
        store.append_audit(
            AuditEventType.WEBHOOK_FILTERED,
            event_id=envelope.event_id,
            detail={
                "reason": reason,
                "payment_id": entity.get("id"),
                "why": "validation artefact, not a recoverable failure",
            },
        )
        return IngestResult(IngestOutcome.FILTERED, envelope.event_id, http_status=200)

    # Raw payload lands before anything else looks at it.
    if not store.record_raw_event(envelope):
        store.append_audit(
            AuditEventType.WEBHOOK_DUPLICATE_IGNORED,
            event_id=envelope.event_id,
            detail={"event": envelope.event, "dedup_key": "x-razorpay-event-id"},
        )
        return IngestResult(IngestOutcome.DUPLICATE, envelope.event_id, http_status=200)

    store.append_audit(
        AuditEventType.WEBHOOK_RECEIVED,
        event_id=envelope.event_id,
        detail={
            "event": envelope.event,
            "created_at": envelope.created_at,
            "payment_id": envelope.payment_entity.get("id"),
        },
    )
    store.enqueue_job(envelope.event_id)
    return IngestResult(IngestOutcome.ACCEPTED, envelope.event_id, http_status=200)
