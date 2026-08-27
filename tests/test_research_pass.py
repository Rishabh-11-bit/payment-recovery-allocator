"""Findings from the research pass, each pinned so a regression names itself."""

from __future__ import annotations

import datetime as dt

import pytest

from recovery.fixtures import build_delivery
from recovery.governor import DowntimeNotice, IssuerState, StormGovernor
from recovery.guard import BlockReason, GuardRequest, ProposalKind, guard_from_config
from recovery.ingest import IngestOutcome, ingest_delivery
from recovery.models import AuditEventType, FailureClass, PaymentStatus
from recovery.normalize import has_nested_error_object, normalize_entity
from recovery.sim.calendar import IST, calendar_from_config
from recovery.sim.world import sample_world
from recovery.store import Store

NOW = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)


@pytest.fixture
def calendar(config):
    return calendar_from_config(config.regulatory)


@pytest.fixture
def guard(config, calendar):
    return guard_from_config(config, calendar)


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "r.db")
    handle.initialise()
    yield handle
    handle.close()


# --------------------------------------------------------- payload shape --- #


def test_error_fields_are_read_flat_off_the_entity():
    """The webhook shape. Nested `error: {}` is the API error-response shape."""
    key = normalize_entity(
        {
            "method": "upi",
            "error_source": "customer_psp",
            "error_step": "payment_debit_response",
            "error_reason": "insufficient_funds",
        }
    )
    assert key.describe() == "upi/customer_psp/payment_debit_response/insufficient_funds"


def test_a_nested_payload_is_detected_rather_than_silently_misread():
    """Without this it degrades to an empty key, unmapped, LOW row -- silent."""
    assert has_nested_error_object({"error": {"source": "x", "step": "y"}})
    assert not has_nested_error_object({"error_source": "x"})
    assert not has_nested_error_object({"error": "a string, not an object"})


def test_a_nested_payload_is_audited_at_ingest(store, config):
    headers, body = build_delivery(event_id="evt_nested")
    entity = body["payload"]["payment"]["entity"]
    for field in ("error_source", "error_step", "error_reason"):
        entity.pop(field, None)
    entity["error"] = {"source": "customer_psp", "step": "payment_debit_response"}

    ingest_delivery(store, config, headers, body)
    assert AuditEventType.PAYLOAD_SHAPE_UNEXPECTED in {
        event.event_type for event in store.audit_trail()
    }


# ------------------------------------------------------- dummy payments --- #


def test_the_registration_dummy_payment_is_filtered(store, config):
    """Always status: failed, never a real failure. One per new mandate."""
    headers, body = build_delivery(
        event_id="evt_dummy",
        error_source="business",
        error_step="payment_initiation",
        error_reason="upi_dummy_payment",
    )
    result = ingest_delivery(store, config, headers, body)

    assert result.outcome is IngestOutcome.FILTERED
    assert result.acknowledged, "a non-2xx would push the webhook toward backoff"
    assert store.raw_event_count() == 0, "it must never enter the batch"
    assert store.pending_job_count() == 0
    assert AuditEventType.WEBHOOK_FILTERED in {
        event.event_type for event in store.audit_trail()
    }


def test_a_real_failure_is_not_filtered(store, config):
    headers, body = build_delivery(event_id="evt_real")
    assert ingest_delivery(store, config, headers, body).outcome is IngestOutcome.ACCEPTED


# --------------------------------------------------------------- timing --- #


def execution(**overrides) -> GuardRequest:
    base = dict(
        kind=ProposalKind.EXECUTION,
        decided_at=NOW,
        execute_at=NOW + dt.timedelta(hours=26),
        attempts_seen=1,
        payment_status=PaymentStatus.FAILED,
        order_id="o",
        rail="upi",
    )
    base.update(overrides)
    return GuardRequest(**base)


def test_upi_needs_twenty_five_hours(guard, calendar):
    assert calendar.pdn_lead_for("upi") == 25
    assert guard.check(execution(execute_at=NOW + dt.timedelta(hours=26))).allowed
    # 24h out: inside the legal window, non-peak, and one hour short of the
    # 25h UPI notice. Under the old flat 24h figure this was compliant.
    too_soon = NOW + dt.timedelta(hours=24)
    assert not calendar.is_peak(too_soon)
    assert guard.check(execution(execute_at=too_soon)).reason is (
        BlockReason.PDN_LEAD_TIME_UNMET
    )


def test_card_needs_thirty_six_hours(guard, calendar):
    """The AFA link is valid 72h above the threshold, so the PDN goes earlier."""
    assert calendar.pdn_lead_for("card") == 36
    late = execution(
        rail="card", execute_at=(NOW + dt.timedelta(hours=26)).replace(hour=3)
    )
    assert guard.check(late).reason is BlockReason.PDN_LEAD_TIME_UNMET


def test_upi_cannot_be_scheduled_past_the_completion_deadline(guard):
    """21:30-24:00 is outside every peak window and still unusable."""
    slot = (NOW + dt.timedelta(days=2)).replace(hour=22, minute=0)
    assert (
        guard.check(execution(execute_at=slot)).reason
        is BlockReason.PAST_UPI_COMPLETION_DEADLINE
    )


def test_the_legal_upi_window_is_the_morning_and_afternoon_blocks(calendar):
    legal = [
        hour
        for hour in range(24)
        if not calendar.is_peak(NOW.replace(hour=hour))
        and not calendar.past_upi_completion_deadline(NOW.replace(hour=hour))
    ]
    assert legal == list(range(0, 10)) + list(range(13, 17))


# ------------------------------------------------------------- counters --- #


def test_auth_attempts_is_authoritative_when_present(guard):
    """Razorpay's docs settle CHALLENGES 008; auth_attempts wins outright."""
    assert (
        guard.check(execution(auth_attempts=4)).reason
        is BlockReason.EXECUTION_CAP_EXHAUSTED
    )
    assert guard.check(execution(attempts_seen=4, auth_attempts=1)).allowed


def test_token_busy_blocks_a_concurrent_operation(guard):
    busy = execution(token_busy_until=NOW + dt.timedelta(days=3))
    assert guard.check(busy).reason is BlockReason.CONCURRENT_REQUEST_IN_PROGRESS


# ----------------------------------------------------- rail-conditional --- #


def test_revocation_is_a_upi_phenomenon():
    """There is no two-tap in-app cancel gesture for a card mandate or e-NACH."""
    world = sample_world(seed=42)
    upi = world.revocation_hazard(FailureClass.LIQUIDITY, 1, "upi")
    card = world.revocation_hazard(FailureClass.LIQUIDITY, 1, "card")
    emandate = world.revocation_hazard(FailureClass.LIQUIDITY, 1, "emandate")

    assert card < upi / 10
    assert emandate == card


def test_paused_and_expired_are_distinct_exit_doors():
    from recovery.sim.environment import CaseOutcome

    assert CaseOutcome.PAUSED.value == "paused"
    assert CaseOutcome.EXPIRED.value == "expired"
    assert len({outcome.value for outcome in CaseOutcome}) == 6


# ------------------------------------------------------ downtime webhook --- #


def test_a_high_severity_notice_degrades_without_waiting_for_observations():
    governor = StormGovernor(min_observations=10)
    governor.observe_downtime(
        DowntimeNotice.from_payload(
            {
                "method": "upi",
                "severity": "high",
                "status": "started",
                "instrument_schema": {"vpa_handle": "okaxis"},
            },
            NOW,
        )
    )
    assert governor.state_of("okaxis", NOW) is IssuerState.DEGRADED


def test_a_resolved_notice_releases_the_instrument():
    governor = StormGovernor(min_observations=10)
    for status in ("started", "resolved"):
        governor.observe_downtime(
            DowntimeNotice.from_payload(
                {
                    "method": "upi",
                    "severity": "high",
                    "status": status,
                    "instrument_schema": {"vpa_handle": "okaxis"},
                },
                NOW,
            )
        )
    assert governor.state_of("okaxis", NOW) is IssuerState.HEALTHY


@pytest.mark.parametrize(
    "method, schema, expected",
    [
        ("upi", {"vpa_handle": "okhdfcbank"}, "okhdfcbank"),
        ("upi", {"psp": "PhonePe"}, "phonepe"),
        ("card", {"issuer": "HDFC"}, "hdfc"),
        ("card", {"network": "VISA"}, "visa"),
        ("netbanking", {"bank": "SBIN"}, "sbin"),
    ],
)
def test_instrument_schema_is_keyed_per_method(method, schema, expected):
    """Keying everything on `issuer` would discard every UPI notice."""
    notice = DowntimeNotice.from_payload(
        {
            "method": method,
            "severity": "low",
            "status": "started",
            "instrument_schema": schema,
        },
        NOW,
    )
    assert notice.instrument == expected


def test_a_scheduled_outage_is_known_in_advance():
    """The only signal here that arrives before the failure it predicts."""
    notice = DowntimeNotice.from_payload(
        {
            "method": "upi",
            "severity": "medium",
            "status": "scheduled",
            "instrument_schema": {"psp": "gpay"},
        },
        NOW,
    )
    assert notice.is_scheduled and notice.is_active


def test_silence_from_the_downtime_feed_is_not_health():
    """A PSP is flagged only when ALL its handles are down -- low recall."""
    assert StormGovernor(min_observations=10).downtime_state("never_reported") is None
