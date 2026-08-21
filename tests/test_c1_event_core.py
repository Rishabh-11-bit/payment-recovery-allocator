"""C1 event core.

The headline test is `test_ten_replays_produce_one_case_and_one_decision` --
that is the definition of done for this component. The rest guard the reasons
the headline holds: state is authoritative rather than payload-derived, ordering
is tolerated, and the append-only tables really are append-only.
"""

from __future__ import annotations

import sqlite3

import pytest

from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway, StateRefreshError
from recovery.ingest import IngestOutcome, ingest_delivery
from recovery.models import AuditEventType, CaseState, PaymentStatus
from recovery.worker import idempotency_key, process_pending


def deliver(store, config, gateway, *, event_id, seed=True, **kwargs):
    headers, body = build_delivery(event_id=event_id, **kwargs)
    if seed:
        gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
    return ingest_delivery(store, config, headers, body)


def audit_types(store, case_id=None) -> list[str]:
    return [event.event_type.value for event in store.audit_trail(case_id)]


# --------------------------------------------------------------- the DoD --- #


def test_ten_replays_produce_one_case_and_one_decision(store, config, gateway, classifier):
    for _ in range(10):
        result = deliver(store, config, gateway, event_id="evt_replay")
        assert result.acknowledged, "every delivery must be acked, duplicates included"

    process_pending(store, config, gateway, classifier)

    assert store.raw_event_count() == 1
    assert store.case_count() == 1
    assert store.decision_count() == 1

    # The whole sequence is present and in order, not just the outcome.
    assert audit_types(store) == (
        [AuditEventType.WEBHOOK_RECEIVED.value]
        + [AuditEventType.WEBHOOK_DUPLICATE_IGNORED.value] * 9
        + [
            AuditEventType.STATE_REFRESHED.value,
            AuditEventType.CASE_OPENED.value,
            AuditEventType.FAILURE_CLASSIFIED.value,
            AuditEventType.DECISION_RECORDED.value,
        ]
    )


def test_replay_is_idempotent_across_worker_runs(store, config, gateway, classifier):
    deliver(store, config, gateway, event_id="evt_1")
    process_pending(store, config, gateway, classifier)
    for _ in range(5):
        deliver(store, config, gateway, event_id="evt_1")
        process_pending(store, config, gateway, classifier)

    assert store.case_count() == 1
    assert store.decision_count() == 1


# ------------------------------------------------------------- dedup ------ #


def test_dedup_key_is_the_header_not_the_body(store, config, gateway, classifier):
    """Same body, different event ids: two events, because the header differs."""
    deliver(store, config, gateway, event_id="evt_a")
    result = deliver(store, config, gateway, event_id="evt_b")

    assert result.outcome is IngestOutcome.ACCEPTED
    assert store.raw_event_count() == 2


def test_delivery_without_event_id_is_rejected_and_not_stored(store, config):
    _, body = build_delivery(event_id="unused")
    result = ingest_delivery(store, config, {"Content-Type": "application/json"}, body)

    assert result.outcome is IngestOutcome.MALFORMED
    assert not result.acknowledged, "a delivery we cannot dedupe must not be silently acked"
    assert store.raw_event_count() == 0


def test_header_case_does_not_matter(store, config, gateway, classifier):
    headers, body = build_delivery(event_id="evt_case")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
    ingest_delivery(store, config, headers, body)

    shouty = {k.upper(): v for k, v in headers.items()}
    assert ingest_delivery(store, config, shouty, body).outcome is IngestOutcome.DUPLICATE


def test_unsupported_event_is_acknowledged_not_rejected(store, config):
    """A non-2xx here would put the whole webhook into 24h backoff."""
    headers, body = build_delivery(event_id="evt_other")
    body["event"] = "payment.captured"
    result = ingest_delivery(store, config, headers, body)

    assert result.outcome is IngestOutcome.UNSUPPORTED
    assert result.acknowledged
    assert store.pending_job_count() == 0


# ---------------------------------------------- authoritative state ------- #


def test_decision_uses_authoritative_state_not_the_payload(store, config, gateway, classifier):
    """The late-authorisation catch: payload says failed, the bank says otherwise."""
    headers, body = build_delivery(event_id="evt_late", payment_id="pay_late")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
    # Three days later, Razorpay's polling turned this into a real payment.
    gateway.set_state("pay_late", status="authorized")

    ingest_delivery(store, config, headers, body)
    stats = process_pending(store, config, gateway, classifier)

    assert stats.decided == 0, "must not spend an attempt on money that already arrived"
    assert stats.closed_resolved == 1
    assert store.decision_count() == 0

    case = store.find_case("order_SYNTH000000001")
    assert case.state is CaseState.CLOSED_PAYMENT_RESOLVED
    assert AuditEventType.CASE_CLOSED_PAYMENT_RESOLVED.value in audit_types(store)


def test_state_is_always_refetched_before_deciding(store, config, gateway, classifier):
    deliver(store, config, gateway, event_id="evt_refresh")
    process_pending(store, config, gateway, classifier)

    assert gateway.fetch_count == 1
    refreshed = [
        event
        for event in store.audit_trail()
        if event.event_type is AuditEventType.STATE_REFRESHED
    ]
    assert len(refreshed) == 1
    assert refreshed[0].detail["authoritative_status"] == PaymentStatus.FAILED.value


def test_refresh_failure_blocks_the_decision(store, config, gateway, classifier):
    """No authoritative state, no decision. The job stays pending for retry."""
    deliver(store, config, gateway, event_id="evt_fail")
    gateway.fail_next = True

    stats = process_pending(store, config, gateway, classifier)

    assert stats.decided == 0
    assert stats.refresh_failures == 1
    assert store.decision_count() == 0
    assert store.pending_job_count() == 1, "must be retryable, not dropped"
    assert AuditEventType.STATE_REFRESH_FAILED.value in audit_types(store)


def test_blocked_decision_is_never_silently_swallowed(store, config, gateway, classifier):
    deliver(store, config, gateway, event_id="evt_silent")
    gateway.fail_next = True
    process_pending(store, config, gateway, classifier)

    # Recovers on the next pass, and both the failure and the success are visible.
    process_pending(store, config, gateway, classifier)

    assert store.decision_count() == 1
    trail = audit_types(store)
    assert trail.index(AuditEventType.STATE_REFRESH_FAILED.value) < trail.index(
        AuditEventType.DECISION_RECORDED.value
    )


# ------------------------------------------------------ ordering ---------- #


def test_out_of_order_event_does_not_regress_the_case(store, config, gateway, classifier):
    deliver(store, config, gateway, event_id="evt_new", created_at=1_755_000_500)
    process_pending(store, config, gateway, classifier)

    # An older event for the same chain arrives afterwards.
    deliver(
        store,
        config,
        gateway,
        event_id="evt_old",
        created_at=1_755_000_100,
        payment_id="pay_older",
    )
    stats = process_pending(store, config, gateway, classifier)

    assert stats.stale_ignored == 1
    assert store.case_count() == 1
    case = store.find_case("order_SYNTH000000001")
    assert case.last_event_created_at == 1_755_000_500
    assert AuditEventType.EVENT_STALE_IGNORED.value in audit_types(store)


def test_events_in_either_order_reach_the_same_case(store, config, gateway, classifier):
    """Two attempts on one order are one chain, whichever arrives first."""
    deliver(store, config, gateway, event_id="evt_2", payment_id="pay_2", created_at=1_755_000_200)
    deliver(store, config, gateway, event_id="evt_1", payment_id="pay_1", created_at=1_755_000_100)
    process_pending(store, config, gateway, classifier)

    assert store.case_count() == 1, "the chain belongs to the order, not the payment"


# ------------------------------------------------------ attempt chain ----- #


def test_attempt_n_is_stable_across_calls(store):
    first = store.assign_attempt_n("order_x", "pay_1")
    assert store.assign_attempt_n("order_x", "pay_1") == first
    assert store.assign_attempt_n("order_x", "pay_2") == first + 1


def test_idempotency_key_shape(store, config, gateway, classifier):
    deliver(store, config, gateway, event_id="evt_key", payment_id="pay_key")
    process_pending(store, config, gateway, classifier)

    case = store.find_case("order_SYNTH000000001")
    keys = [row["idempotency_key"] for row in store.decisions_for_case(case.case_id)]
    assert keys == [idempotency_key("pay_key", config.policy.version, 1)]
    assert keys[0] == f"recovery:pay_key:{config.policy.version}:1"


def test_policy_version_bump_permits_a_new_decision(store, config, gateway, classifier):
    """Bumping the version deliberately re-opens a decision already taken."""
    deliver(store, config, gateway, event_id="evt_v1", payment_id="pay_v")
    process_pending(store, config, gateway, classifier)

    bumped = config.model_copy(
        update={"policy": config.policy.model_copy(update={"version": "0.2.0"})}
    )
    deliver(store, config, gateway, event_id="evt_v2", payment_id="pay_v")
    process_pending(store, bumped, gateway, classifier)

    assert store.decision_count() == 2


# ------------------------------------------------------ append-only ------- #


@pytest.mark.parametrize("table", ["raw_events", "decisions", "audit_events"])
def test_append_only_tables_reject_mutation(store, config, gateway, classifier, table):
    deliver(store, config, gateway, event_id="evt_immutable")
    process_pending(store, config, gateway, classifier)

    for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET rowid = rowid"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(statement)


def test_raw_payload_is_stored_before_parsing(store, config, gateway, classifier):
    """A payload we cannot interpret is still preserved."""
    headers, body = build_delivery(event_id="evt_weird")
    body["payload"]["payment"]["entity"]["error_step"] = "a_step_we_have_never_seen"
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])

    assert ingest_delivery(store, config, headers, body).outcome is IngestOutcome.ACCEPTED
    stored = store._conn.execute("SELECT body_json FROM raw_events").fetchone()[0]
    assert "a_step_we_have_never_seen" in stored


# ------------------------------------------------------ ack speed --------- #


def test_ingest_does_not_touch_the_gateway(store, config, gateway, classifier):
    """Ack fast: the slow call belongs in the worker, not the request path."""
    deliver(store, config, gateway, event_id="evt_fast")
    assert gateway.fetch_count == 0

    process_pending(store, config, gateway, classifier)
    assert gateway.fetch_count == 1


def test_ingest_stays_within_the_ack_budget(store, config, gateway, classifier):
    import time

    start = time.perf_counter()
    for index in range(50):
        deliver(store, config, gateway, event_id=f"evt_speed_{index}")
    elapsed = time.perf_counter() - start

    # Razorpay's timeout is per delivery; 50 in one budget is a wide margin.
    assert elapsed < config.ingest.ack_budget_seconds


# ------------------------------------------------------ chain fallback ---- #


def test_payment_without_an_order_is_audited_not_absorbed(store, config, gateway, classifier):
    deliver(store, config, gateway, event_id="evt_noorder", payment_id="pay_no", order_id=None)
    process_pending(store, config, gateway, classifier)

    case = store.find_case("payment:pay_no")
    assert case is not None and case.order_id is None
    opened = [
        event
        for event in store.audit_trail()
        if event.event_type is AuditEventType.CASE_OPENED
    ]
    assert opened[0].detail["order_scoped"] is False


def test_unknown_payment_surfaces_as_a_refresh_error(config):
    gateway = SimulatedGateway()
    with pytest.raises(StateRefreshError):
        gateway.fetch_payment("pay_does_not_exist")
