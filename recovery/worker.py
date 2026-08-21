"""Worker: the only place a decision is taken.

Order of operations matters here, and each step is load-bearing:

1. **Refresh authoritative state.** Before anything else. A `payment.failed`
   payload is provisional -- Razorpay polls the bank for ~3 days and the payment
   may now be authorized. If the refresh fails and `require_state_refresh` is
   set, we do not decide at all. Deciding from the payload would be exactly the
   failure the safety invariant exists to prevent.
2. **Resolve the chain.** A case is keyed on `order_id`, because the attempt
   chain belongs to the order, not to any one payment.
3. **Reject stale events.** Delivery can arrive out of order; an older event
   must never regress case state.
4. **Stop if the payment resolved.** No decision on money that already arrived.
5. **Decide, once.** Uniqueness on the idempotency key is what makes it once.

The decision *content* is C3's and is hand-authored. This module owns only the
seam: `Decider`. C1 ships `PendingClassifierDecider`, which returns HOLD for
everything -- a placeholder with no policy in it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from recovery.config import Config
from recovery.gateway import PaymentGateway, StateRefreshError
from recovery.models import (
    AuditEventType,
    Case,
    CaseState,
    Decision,
    DecisionAction,
    PaymentSnapshot,
    WebhookEnvelope,
)
from recovery.store import Store


def idempotency_key(payment_id: str, policy_version: str, attempt_n: int) -> str:
    return f"recovery:{payment_id}:{policy_version}:{attempt_n}"


class Decider(Protocol):
    """The allocator's interface (C3). Hand-authored -- not implemented here."""

    def decide(self, case: Case, snapshot: PaymentSnapshot, attempt_n: int) -> tuple[
        DecisionAction, str
    ]: ...


class PendingClassifierDecider:
    """C1 placeholder. HOLD for everything, with the reason stated plainly.

    This is deliberately not a policy. It exists so the event core can be proved
    exactly-once before the classifier (C2) and allocator (C3) exist. Replacing
    it must not require changing anything in this module.
    """

    def decide(
        self, case: Case, snapshot: PaymentSnapshot, attempt_n: int
    ) -> tuple[DecisionAction, str]:
        return DecisionAction.HOLD, "pending_classifier: C2/C3 not yet wired"


@dataclass
class WorkerStats:
    processed: int = 0
    decided: int = 0
    duplicates_suppressed: int = 0
    stale_ignored: int = 0
    closed_resolved: int = 0
    refresh_failures: int = 0


def process_pending(
    store: Store,
    config: Config,
    gateway: PaymentGateway,
    decider: Decider | None = None,
) -> WorkerStats:
    """Drain the pending queue once. Returns what happened, for the reproduce run."""
    decider = decider or PendingClassifierDecider()
    stats = WorkerStats()

    for row in store.claim_jobs(config.worker.batch_size):
        envelope = WebhookEnvelope(
            event_id=row["event_id"],
            event="payment.failed",
            created_at=row["created_at"],
            headers=json.loads(row["headers_json"]),
            body=json.loads(row["body_json"]),
        )
        stats.processed += 1
        _process_one(store, config, gateway, decider, envelope, row["job_id"], stats)

    return stats


def _process_one(
    store: Store,
    config: Config,
    gateway: PaymentGateway,
    decider: Decider,
    envelope: WebhookEnvelope,
    job_id: str,
    stats: WorkerStats,
) -> None:
    entity = envelope.payment_entity
    payment_id = entity.get("id")
    if not payment_id:
        store.append_audit(
            AuditEventType.WEBHOOK_REJECTED_UNSUPPORTED,
            event_id=envelope.event_id,
            detail={"reason": "no payment id in payload"},
        )
        store.finish_job(job_id, "failed")
        return

    # 1. Authoritative state, before any decision. Never the payload.
    try:
        snapshot = gateway.fetch_payment(payment_id)
    except StateRefreshError as exc:
        stats.refresh_failures += 1
        store.append_audit(
            AuditEventType.STATE_REFRESH_FAILED,
            event_id=envelope.event_id,
            detail={"payment_id": payment_id, "error": str(exc)},
        )
        if config.late_auth.require_state_refresh:
            # Back to pending. Not deciding is the correct outcome, not a failure
            # to handle -- and it is visible in the trail either way.
            store.finish_job(job_id, "pending")
            return
        raise

    store.append_audit(
        AuditEventType.STATE_REFRESHED,
        event_id=envelope.event_id,
        detail={
            "payment_id": payment_id,
            "authoritative_status": snapshot.status.value,
            "payload_status": entity.get("status"),
            "diverged": entity.get("status") != snapshot.status.value,
        },
    )

    # 2. The chain owns the case. order_id where present.
    if snapshot.order_id:
        chain_key, order_id = snapshot.order_id, snapshot.order_id
    else:
        # Should not happen for mandate debits. Audited rather than absorbed.
        chain_key, order_id = f"payment:{payment_id}", None

    case, opened = store.open_case(chain_key, order_id, payment_id, envelope.created_at)
    store.append_audit(
        AuditEventType.CASE_OPENED if opened else AuditEventType.CASE_ATTACHED,
        case_id=case.case_id,
        event_id=envelope.event_id,
        detail={
            "chain_key": chain_key,
            "payment_id": payment_id,
            "order_scoped": order_id is not None,
        },
    )

    # 3. Out-of-order delivery must not regress state.
    if envelope.created_at < case.last_event_created_at:
        stats.stale_ignored += 1
        store.append_audit(
            AuditEventType.EVENT_STALE_IGNORED,
            case_id=case.case_id,
            event_id=envelope.event_id,
            detail={
                "event_created_at": envelope.created_at,
                "case_last_event_created_at": case.last_event_created_at,
            },
        )
        store.finish_job(job_id, "done")
        return

    # 4. Money already arrived. This is the late-authorisation catch.
    if snapshot.status.is_resolved:
        stats.closed_resolved += 1
        store.advance_case(
            case.case_id, CaseState.CLOSED_PAYMENT_RESOLVED, envelope.created_at
        )
        store.append_audit(
            AuditEventType.CASE_CLOSED_PAYMENT_RESOLVED,
            case_id=case.case_id,
            event_id=envelope.event_id,
            detail={"payment_id": payment_id, "status": snapshot.status.value},
        )
        store.finish_job(job_id, "done")
        return

    # 5. Decide, exactly once.
    attempt_n = store.assign_attempt_n(chain_key, payment_id)
    action, reason = decider.decide(case, snapshot, attempt_n)
    decision = Decision(
        idempotency_key=idempotency_key(payment_id, config.policy.version, attempt_n),
        case_id=case.case_id,
        payment_id=payment_id,
        policy_version=config.policy.version,
        attempt_n=attempt_n,
        action=action,
        reason=reason,
        decided_at=datetime.now(timezone.utc),
    )

    if store.record_decision(decision):
        stats.decided += 1
        store.append_audit(
            AuditEventType.DECISION_RECORDED,
            case_id=case.case_id,
            event_id=envelope.event_id,
            detail={
                "idempotency_key": decision.idempotency_key,
                "action": action.value,
                "reason": reason,
                "attempt_n": attempt_n,
            },
        )
        store.advance_case(case.case_id, CaseState.DECIDED, envelope.created_at)
    else:
        stats.duplicates_suppressed += 1
        store.append_audit(
            AuditEventType.DECISION_DUPLICATE_SUPPRESSED,
            case_id=case.case_id,
            event_id=envelope.event_id,
            detail={"idempotency_key": decision.idempotency_key},
        )

    store.finish_job(job_id, "done")
