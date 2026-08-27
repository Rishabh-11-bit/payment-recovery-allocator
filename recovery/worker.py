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

from recovery.classifier import Classifier
from recovery.config import Config
from recovery.gateway import PaymentGateway, StateRefreshError
from recovery.guard import Guard, GuardRequest, ProposalKind
from recovery.models import (
    AuditEventType,
    Case,
    CaseState,
    Classification,
    Decision,
    DecisionAction,
    PaymentSnapshot,
    WebhookEnvelope,
)
from recovery.normalize import normalize_snapshot
from recovery.store import Store


def idempotency_key(payment_id: str, policy_version: str, attempt_n: int) -> str:
    return f"recovery:{payment_id}:{policy_version}:{attempt_n}"


class Decider(Protocol):
    """The allocator's interface (C3). Hand-authored -- not implemented here.

    Receives the classification rather than the raw failure: the allocator
    branches on class and on confidence *band*, not on the payload. In
    particular `classification.may_exclude_instrument` is the HIGH-band gate --
    exclusion on a misdiagnosis makes recovery harder, so reorder is the default.
    """

    def decide(
        self,
        case: Case,
        snapshot: PaymentSnapshot,
        classification: Classification,
        attempt_n: int,
    ) -> tuple[DecisionAction, str]: ...


# Optional, and looked up by name rather than declared on the protocol above.
#
# A decider that schedules an execution has already chosen *when* -- picking a
# compliant slot is most of what deciding to retry means. The guard then has to
# be told, because "an execution must name when it will run" is one of the
# things it checks, and a decider that knows the slot but cannot hand it over
# gets its own valid decisions refused.
#
# It is optional because a decider that never schedules an execution -- the
# placeholder, and any contact-only policy -- has no slot to give and should not
# be forced to implement a method returning None.
SLOT_HOOK = "execution_slot"


class PendingAllocatorDecider:
    """Placeholder. HOLD for everything, with the reason stated plainly.

    Deliberately not a policy. It exists so the event core and classifier can be
    proved before the allocator (C3) exists. Replacing it must not require
    changing anything in this module.
    """

    def decide(
        self,
        case: Case,
        snapshot: PaymentSnapshot,
        classification: Classification,
        attempt_n: int,
    ) -> tuple[DecisionAction, str]:
        return (
            DecisionAction.HOLD,
            f"pending_allocator: classified {classification.failure_class.value} "
            f"({classification.band.value}), C3 not yet wired",
        )


@dataclass
class WorkerStats:
    processed: int = 0
    decided: int = 0
    duplicates_suppressed: int = 0
    guard_blocks: int = 0
    stale_ignored: int = 0
    closed_resolved: int = 0
    refresh_failures: int = 0
    unmapped_failures: int = 0


# Actions that create a payment obligation, and therefore need admission.
# HOLD and SURRENDER create none, so there is nothing for the guard to admit.
OBLIGATION_KIND = {
    DecisionAction.SCHEDULE_AT: ProposalKind.EXECUTION,
    DecisionAction.RECOVERY_LINK: ProposalKind.CONTACT,
    DecisionAction.OFFER_RAIL_MIGRATION: ProposalKind.CONTACT,
    DecisionAction.REORDER_RAILS: ProposalKind.CONTACT,
    DecisionAction.EXCLUDE_INSTRUMENT: ProposalKind.CONTACT,
}


def process_pending(
    store: Store,
    config: Config,
    gateway: PaymentGateway,
    classifier: Classifier,
    decider: Decider | None = None,
    guard: Guard | None = None,
) -> WorkerStats:
    """Drain the pending queue once. Returns what happened, for the reproduce run."""
    decider = decider or PendingAllocatorDecider()
    stats = WorkerStats()

    for row in store.claim_jobs(
        config.worker.batch_size,
        claim_timeout_seconds=config.worker.claim_timeout_seconds,
        max_attempts=config.worker.max_attempts_per_job,
    ):
        envelope = WebhookEnvelope(
            event_id=row["event_id"],
            event="payment.failed",
            created_at=row["created_at"],
            headers=json.loads(row["headers_json"]),
            body=json.loads(row["body_json"]),
        )
        stats.processed += 1
        _process_one(
            store,
            config,
            gateway,
            classifier,
            decider,
            envelope,
            row["job_id"],
            stats,
            guard,
        )

    return stats


def _classify(
    store: Store,
    classifier: Classifier,
    snapshot: PaymentSnapshot,
    case_id: str,
    event_id: str,
    stats: WorkerStats,
) -> Classification:
    """Normalize, classify, and make both visible in the trail."""
    key = normalize_snapshot(
        snapshot,
        source_space=classifier.config.source_space,
        source_aliases=classifier.config.source_aliases,
    )

    if key.missing or key.aliases_applied:
        store.append_audit(
            AuditEventType.FAILURE_NORMALIZED,
            case_id=case_id,
            event_id=event_id,
            detail={
                "key": key.describe(),
                "missing": list(key.missing),
                "aliases_applied": [list(pair) for pair in key.aliases_applied],
            },
        )

    if not key.source_in_documented_space:
        # Surfaced for review, not rejected. The documented value space is a
        # lower bound -- see CHALLENGES 007. Classification proceeds normally
        # and this event exists so the gap between doc and reality stays visible
        # rather than silently absorbed.
        store.append_audit(
            AuditEventType.FAILURE_SOURCE_UNDOCUMENTED,
            case_id=case_id,
            event_id=event_id,
            detail={
                "method": key.method,
                "source": key.source,
                "documented": sorted(classifier.config.source_space.get(key.method or "", [])),
                "action": "surfaced_for_review",
            },
        )

    classification = classifier.classify(key)

    if not classification.mapped:
        stats.unmapped_failures += 1
        store.append_audit(
            AuditEventType.FAILURE_UNMAPPED,
            case_id=case_id,
            event_id=event_id,
            detail={
                "key": key.describe(),
                "fell_back_to": classification.failure_class.value,
            },
        )

    if classification.cost_resolved_from is not None:
        store.append_audit(
            AuditEventType.CLASSIFICATION_COST_RESOLVED,
            case_id=case_id,
            event_id=event_id,
            detail={
                "predicted": classification.cost_resolved_from.value,
                "resolved_to": classification.failure_class.value,
                "confidence": classification.confidence,
                "basis": "lowest worst-case cost, not highest likelihood",
            },
        )

    store.append_audit(
        AuditEventType.FAILURE_CLASSIFIED,
        case_id=case_id,
        event_id=event_id,
        detail={
            "key": key.describe(),
            "class": classification.failure_class.value,
            "confidence": classification.confidence,
            "band": classification.band.value,
            "mapped": classification.mapped,
            "rule_index": classification.rule_index,
            "may_exclude_instrument": classification.may_exclude_instrument,
            "cause_family": classification.cause_family,
            "source_undocumented": classification.source_undocumented,
        },
    )
    return classification


def _process_one(
    store: Store,
    config: Config,
    gateway: PaymentGateway,
    classifier: Classifier,
    decider: Decider,
    envelope: WebhookEnvelope,
    job_id: str,
    stats: WorkerStats,
    guard: Guard | None = None,
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

    # 5. Classify, then decide exactly once.
    classification = _classify(
        store, classifier, snapshot, case.case_id, envelope.event_id, stats
    )
    attempt_n = store.assign_attempt_n(chain_key, payment_id)
    action, reason = decider.decide(case, snapshot, classification, attempt_n)

    # 6. Admission control. Only actions that create a payment obligation need
    #    it -- HOLD and SURRENDER create none, so there is nothing to admit.
    kind = OBLIGATION_KIND.get(action)
    if guard is not None and kind is not None:
        slot_hook = getattr(decider, SLOT_HOOK, None)
        execute_at = (
            slot_hook(case, snapshot, attempt_n)
            if kind is ProposalKind.EXECUTION and slot_hook is not None
            else None
        )
        request = GuardRequest(
            kind=kind,
            decided_at=datetime.now(timezone.utc),
            execute_at=execute_at,
            attempts_seen=attempt_n,
            contacts_seen=0,
            payment_status=snapshot.status,
            order_id=snapshot.order_id,
            order_expires_at=snapshot.order_expires_at,
            # The PDN lead time is rail-conditional -- 25h for UPI, 36h for
            # cards -- so the guard cannot check it without knowing the rail.
            rail=snapshot.method,
        )
        verdict = guard.check(request)
        if verdict.blocked:
            stats.guard_blocks += 1
            store.append_audit(
                AuditEventType.GUARD_BLOCKED,
                case_id=case.case_id,
                event_id=envelope.event_id,
                detail={
                    "action": action.value,
                    "reason": verdict.reason.value,
                    "detail": verdict.detail,
                },
            )
            store.finish_job(job_id, "done")
            return
        store.append_audit(
            AuditEventType.GUARD_ALLOWED,
            case_id=case.case_id,
            event_id=envelope.event_id,
            detail={"action": action.value, "kind": kind.value},
        )

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
