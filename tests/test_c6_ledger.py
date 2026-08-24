"""C6 -- the audit ledger and the decision trace.

The trail already existed; these test that it can be *read*. A trail nobody can
query is a compliance artefact, and the question a panel asks is "why did this
case get this decision", not "is there a table".
"""

from __future__ import annotations

import datetime as dt

import pytest

from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway
from recovery.guard import guard_from_config
from recovery.ingest import ingest_delivery
from recovery.ledger import Ledger, render_trace
from recovery.models import AuditEventType
from recovery.sim.calendar import calendar_from_config
from recovery.store import Store
from recovery.worker import process_pending


@pytest.fixture
def populated(tmp_path, config, classifier):
    """One case, ten deliveries, one decision -- the C1 definition of done."""
    store = Store(tmp_path / "ledger.db")
    store.initialise()
    gateway = SimulatedGateway()
    headers, body = build_delivery(event_id="evt_led", payment_id="pay_led",
                                   order_id="order_led")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
    for _ in range(10):
        ingest_delivery(store, config, headers, body)
    process_pending(store, config, gateway, classifier)
    yield store
    store.close()


@pytest.fixture
def ledger(populated):
    return Ledger(populated)


# ---------------------------------------------------------------- resolve -- #


def test_resolves_by_case_id(ledger):
    case_id = ledger.case_ids()[0]
    assert ledger.resolve(case_id) == case_id


def test_resolves_by_order_id(ledger):
    """A merchant complains with an order id."""
    assert ledger.resolve("order_led") == ledger.case_ids()[0]


def test_resolves_by_payment_id(ledger):
    """A customer complains with a payment id, which is the common case."""
    assert ledger.resolve("pay_led") == ledger.case_ids()[0]


def test_unknown_identifier_resolves_to_nothing(ledger):
    assert ledger.resolve("pay_never_seen") is None


# ------------------------------------------------------------------ trace -- #


def test_trace_includes_pre_case_events(ledger):
    """webhook.received and the duplicates happen before a case exists.

    Reading only by case id loses where the case came from, and hides the
    duplicates entirely -- which are the evidence that at-least-once delivery
    was handled rather than nine problems.
    """
    trace = ledger.trace(ledger.case_ids()[0])
    kinds = {e.event_type for e in trace.events}
    assert AuditEventType.WEBHOOK_RECEIVED in kinds
    assert AuditEventType.WEBHOOK_DUPLICATE_IGNORED in kinds
    assert AuditEventType.STATE_REFRESHED in kinds
    assert trace.duplicates == 9


def test_trace_events_are_ordered(ledger):
    """Ordering is the evidence: a refresh after a decision would be a bug."""
    trace = ledger.trace(ledger.case_ids()[0])
    seqs = [e.seq for e in trace.events]
    assert seqs == sorted(seqs)

    refresh = next(
        e.seq for e in trace.events if e.event_type is AuditEventType.STATE_REFRESHED
    )
    decision = next(
        e.seq for e in trace.events if e.event_type is AuditEventType.DECISION_RECORDED
    )
    assert refresh < decision, "state was refreshed before the decision, not after"


def test_trace_carries_the_decision_and_its_key(ledger):
    trace = ledger.trace(ledger.case_ids()[0])
    assert len(trace.decisions) == 1
    assert trace.decisions[0].idempotency_key.startswith("recovery:pay_led:")


def test_trace_carries_the_chain(ledger):
    trace = ledger.trace(ledger.case_ids()[0])
    assert trace.chain_key == "order_led"
    assert trace.payments == ("pay_led",)


def test_missing_case_traces_to_nothing(ledger):
    assert ledger.trace("case_does_not_exist") is None


# ---------------------------------------------------------------- outcome -- #


def test_outcome_names_the_decision(ledger):
    trace = ledger.trace(ledger.case_ids()[0])
    assert "HOLD" in trace.outcome_line()


def test_a_settled_payment_says_why_nothing_happened(tmp_path, config, classifier):
    """"No decision" and "the payment had already settled" are different facts."""
    store = Store(tmp_path / "settled.db")
    store.initialise()
    gateway = SimulatedGateway()
    headers, body = build_delivery(event_id="evt_late", payment_id="pay_late")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
    gateway.set_state("pay_late", status="authorized")
    ingest_delivery(store, config, headers, body)
    process_pending(store, config, gateway, classifier)

    ledger = Ledger(store)
    trace = ledger.trace(ledger.case_ids()[0])
    assert not trace.decisions
    assert "already settled" in trace.outcome_line()
    store.close()


def test_a_refused_decision_is_distinguishable_from_no_decision(
    tmp_path, config, classifier
):
    """A guard block is part of the story, not an absence of one."""
    store = Store(tmp_path / "blocked.db")
    store.initialise()
    gateway = SimulatedGateway()
    headers, body = build_delivery(event_id="evt_block", payment_id="pay_block")
    entity = body["payload"]["payment"]["entity"]
    gateway.seed_from_webhook(entity)
    # An order that expired before the decision was taken.
    gateway.set_state(
        "pay_block",
        order_expires_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
    )
    ingest_delivery(store, config, headers, body)

    calendar = calendar_from_config(config.regulatory)

    class AlwaysLink:
        def decide(self, case, snapshot, classification, attempt_n):
            from recovery.models import DecisionAction

            return DecisionAction.RECOVERY_LINK, "probe"

    process_pending(
        store,
        config,
        gateway,
        classifier,
        decider=AlwaysLink(),
        guard=guard_from_config(config, calendar),
    )

    ledger = Ledger(store)
    trace = ledger.trace(ledger.case_ids()[0])
    assert not trace.decisions
    assert trace.blocked, "the block should be in the trail"
    assert "guard blocked" in trace.outcome_line()
    assert "order_expired" in trace.outcome_line()
    store.close()


# --------------------------------------------------------------- rendering - #


def test_render_hides_routine_events_and_says_how_many(ledger):
    rendered = render_trace(ledger.trace(ledger.case_ids()[0]))
    assert "routine hidden" in rendered
    assert "duplicate deliveries ignored" in rendered
    assert "not a fault" in rendered


def test_verbose_shows_every_event(ledger):
    trace = ledger.trace(ledger.case_ids()[0])
    rendered = render_trace(trace, verbose=True)
    for event in trace.events:
        assert str(event.seq) in rendered


def test_compact_view_is_a_filter_not_a_different_record(ledger):
    """Both views read the same append-only tables."""
    trace = ledger.trace(ledger.case_ids()[0])
    compact = render_trace(trace)
    verbose = render_trace(trace, verbose=True)
    assert f"trail ({len(trace.events)} events" in compact
    assert f"trail ({len(trace.events)} events)" in verbose


def test_render_names_the_classification(ledger):
    rendered = render_trace(ledger.trace(ledger.case_ids()[0]))
    assert "classified" in rendered
    assert "upi/customer_psp/payment_debit_response" in rendered


# --------------------------------------------------------------- summary --- #


def test_summary_counts_the_ledger(ledger):
    summary = ledger.summary()
    assert summary["cases"] == 1
    assert summary["decisions"] == 1
    assert summary["raw_events"] == 1
    assert summary["audit_events"] >= 13


def test_event_counts_are_ranked(ledger):
    counts = ledger.event_counts()
    assert counts
    assert [c for _, c in counts] == sorted([c for _, c in counts], reverse=True)


def test_block_reasons_aggregate(tmp_path, config, classifier):
    store = Store(tmp_path / "blocks.db")
    store.initialise()
    from recovery.store import _now

    for index in range(3):
        store.append_audit(
            AuditEventType.GUARD_BLOCKED,
            case_id=f"case_{index}",
            detail={"reason": "order_expired", "detail": "x"},
        )
    store.append_audit(
        AuditEventType.GUARD_BLOCKED, case_id="case_9",
        detail={"reason": "peak_hour_barred", "detail": "y"},
    )
    reasons = dict(Ledger(store).block_reasons())
    assert reasons == {"order_expired": 3, "peak_hour_barred": 1}
    store.close()


def test_ledger_has_no_write_path():
    """Read-only by construction, not by convention.

    AST rather than a text scan: the module docstring explains that the tables
    block UPDATE and DELETE, and a naive grep flags its own explanation.
    """
    import ast
    import pathlib as _pathlib

    tree = ast.parse(
        _pathlib.Path("recovery/ledger.py").read_text(encoding="utf-8")
    )
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            head = node.value.strip().upper()
            if head.startswith(("INSERT", "UPDATE", "DELETE", "DROP", "ALTER")):
                writes.append(node.value[:40])
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "append_audit":
                writes.append("append_audit(...)")
    assert not writes, f"ledger contains a write path: {writes}"
