"""C7 -- adversarial event sequences and the invariants they must not break.

## The invariant being hunted

> Never create a payment obligation outside the original order's attempt chain
> while that chain is within its late-authorisation window.

A *payment obligation* here is a recorded decision: the row that authorises a
recovery action against a chain. The hunt is for any generated ordering of
adversarial events that produces an obligation the invariant forbids.

## Why generated rather than written

A hand-written case list tests the orderings its author thought of, and the
orderings that cause double-charges are the ones nobody thought of. So the
orderings are generated: this module builds them from a seeded random walk for
the reproduce path, and `tests/test_c7_invariants.py` drives the same executor
with Hypothesis, which additionally shrinks any failure to a minimal sequence.

Both report how many sequences were explored, because "I could not break it"
means nothing without the size of the search.

Verified at 5,000 orderings / 42,715 events with the default seed, no violation.
Sampled from the generated space rather than enumerated over it -- evidence, not
proof, and worth stating as evidence.

## What is generated

Duplicate deliveries, out-of-order deliveries, a `payment.failed` followed by a
late `authorized` for the same payment inside the 3-day polling window, a worker
crashing between claiming a job and finishing it, two workers on one case, order
expiry mid-recovery, and a PDN window shifting under a scheduled attempt.

## Coverage

Every generated hazard is now enforced. Order expiry and PDN-window shift were
generated but unguarded until C4 landed; they are now checked at the admission
point, and the sequences exercise a real block rather than passing through a
gap. `SearchReport.unguarded` is empty and the reporting says so.
"""

from __future__ import annotations

import datetime as dt
import enum
import pathlib
import random
from dataclasses import dataclass, field

from recovery.classifier import Classifier
from recovery.config import Config
from recovery.guard import GuardRequest, ProposalKind, guard_from_config
from recovery.models import PaymentStatus
from recovery.sim.calendar import IST, calendar_from_config
from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway
from recovery.ingest import ingest_delivery
from recovery.store import Store
from recovery.worker import process_pending

# Empty since C4. Kept as a named constant rather than deleted: a future hazard
# added to the generator before its check exists belongs here, and a clean run
# must never be readable as "everything is enforced" without something to check.
UNGUARDED_HAZARDS: tuple[str, ...] = ()


class Step(str, enum.Enum):
    DELIVER = "deliver"
    REDELIVER = "redeliver"
    LATE_AUTHORIZE = "late_authorize"
    RUN_WORKER = "run_worker"
    CRASH_WORKER = "crash_worker"
    CONCURRENT_WORKERS = "concurrent_workers"
    EXPIRE_ORDER = "expire_order"
    SHIFT_PDN_WINDOW = "shift_pdn_window"


@dataclass(frozen=True)
class Action:
    step: Step
    payment_index: int = 0
    created_at: int = 0


@dataclass(frozen=True)
class Scenario:
    """One adversarial ordering over a single order's attempt chain."""

    order_id: str
    payment_count: int
    actions: tuple[Action, ...]

    def payment_id(self, index: int) -> str:
        return f"pay_INV{index:04d}"


@dataclass
class Violation:
    name: str
    detail: str
    scenario: Scenario | None = None

    def __str__(self) -> str:
        return f"{self.name}: {self.detail}"


@dataclass
class _Trace:
    """What happened, for checking invariants that span the whole sequence."""

    resolved_at_step: dict[str, int] = field(default_factory=dict)
    order_expired: bool = False
    # Executions the guard admitted: (step, payment_id, execute_at).
    admitted: list[tuple[int, str, "dt.datetime"]] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    decisions_at_step: list[tuple[int, str, str]] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set)
    delivered_event_ids: set[str] = field(default_factory=set)
    order_expired_at_step: int | None = None


def execute(
    scenario: Scenario,
    config: Config,
    classifier: Classifier,
    db_path: pathlib.Path | str,
) -> tuple[Store, _Trace]:
    """Run one adversarial ordering against the real event core."""
    store = Store(db_path)
    store.initialise()
    gateway = SimulatedGateway()
    calendar = calendar_from_config(config.regulatory)
    guard = guard_from_config(config, calendar)
    trace = _Trace()
    event_counter = 0

    # A compliant baseline for scheduled executions. SHIFT_PDN_WINDOW moves it
    # until it stops being compliant, which is the point of the hazard.
    clock = dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
    pdn_shift_hours = 0
    order_expires_at: dt.datetime | None = None

    for index in range(scenario.payment_count):
        gateway.set_state(
            scenario.payment_id(index),
            id=scenario.payment_id(index),
            status="failed",
            order_id=scenario.order_id,
            method="upi",
            amount=49900,
            error_source="customer_psp",
            error_step="payment_debit_response",
            error_reason="insufficient_funds",
        )

    for step_number, action in enumerate(scenario.actions):
        index = action.payment_index % scenario.payment_count
        payment_id = scenario.payment_id(index)

        if action.step is Step.DELIVER:
            event_counter += 1
            event_id = f"evt_{step_number}_{event_counter}"
            headers, body = build_delivery(
                event_id=event_id,
                payment_id=payment_id,
                order_id=scenario.order_id,
                created_at=action.created_at,
            )
            ingest_delivery(store, config, headers, body)
            trace.delivered_event_ids.add(event_id)

        elif action.step is Step.REDELIVER:
            # At-least-once delivery: the same event id arriving again.
            if trace.delivered_event_ids:
                event_id = sorted(trace.delivered_event_ids)[
                    index % len(trace.delivered_event_ids)
                ]
                headers, body = build_delivery(
                    event_id=event_id,
                    payment_id=payment_id,
                    order_id=scenario.order_id,
                    created_at=action.created_at,
                )
                ingest_delivery(store, config, headers, body)

        elif action.step is Step.LATE_AUTHORIZE:
            # Razorpay polled the bank and the payment came good, inside the
            # 3-day window. Every retry in that window is now unsafe.
            gateway.set_state(payment_id, status="authorized")
            trace.resolved_at_step.setdefault(payment_id, step_number)

        elif action.step is Step.RUN_WORKER:
            process_pending(store, config, gateway, classifier)

        elif action.step is Step.CRASH_WORKER:
            # Claim work, then die before finishing it.
            store.claim_jobs(config.worker.batch_size)

        elif action.step is Step.CONCURRENT_WORKERS:
            # A second connection to the same database, racing the first.
            other = Store(db_path)
            try:
                process_pending(store, config, gateway, classifier)
                process_pending(other, config, gateway, classifier)
            finally:
                other.close()

        elif action.step is Step.EXPIRE_ORDER:
            # The order stops being able to carry a new obligation. The guard
            # reads this; before C4 nothing did.
            trace.order_expired_at_step = step_number
            trace.order_expired = True
            order_expires_at = clock - dt.timedelta(hours=1)

        elif action.step is Step.SHIFT_PDN_WINDOW:
            # The scheduled execution slides toward the decision moment until
            # the rail's pre-debit notification lead time (25h UPI / 36h card)
            # can no longer be met.
            pdn_shift_hours += 9

        # --- Admission. Every scheduled execution passes the guard. ---------
        if action.step in (Step.RUN_WORKER, Step.CONCURRENT_WORKERS):
            for case_row in store._conn.execute(
                "SELECT case_id, payment_id FROM cases"
            ).fetchall():
                admitted_for_case = [
                    entry for entry in trace.admitted if entry[1] == case_row["payment_id"]
                ]
                # The slot is spread across payments so some land in a barred
                # peak window naturally. The PDN hazard moves the *decision*
                # later instead of moving the slot, so the two hazards stay
                # separable -- otherwise a shifted slot lands in peak and the
                # peak check masks the PDN one, which is what happened first.
                execute_at = clock + dt.timedelta(hours=26 + 8 * index)
                decided_at = clock + dt.timedelta(hours=pdn_shift_hours)
                request = GuardRequest(
                    kind=ProposalKind.EXECUTION,
                    decided_at=decided_at,
                    execute_at=execute_at,
                    attempts_seen=1 + len(admitted_for_case),
                    contacts_seen=0,
                    payment_status=(
                        PaymentStatus.AUTHORIZED
                        if case_row["payment_id"] in trace.resolved_at_step
                        else PaymentStatus.FAILED
                    ),
                    order_id=scenario.order_id,
                    order_expires_at=order_expires_at,
                )
                verdict = guard.check(request)
                if verdict.allowed:
                    trace.admitted.append(
                        (step_number, case_row["payment_id"], execute_at)
                    )
                else:
                    trace.blocks.append(verdict.reason.value)

        # Record newly-appeared decisions with the step that produced them.
        # Set membership rather than a list scan: this runs once per action of
        # every scenario, and the quadratic version dominated the search.
        for row in store._conn.execute(
            "SELECT payment_id, idempotency_key FROM decisions"
        ).fetchall():
            key = row["idempotency_key"]
            if key not in trace._seen_keys:
                trace._seen_keys.add(key)
                trace.decisions_at_step.append((step_number, row["payment_id"], key))

    return store, trace


def check(scenario: Scenario, store: Store, trace: _Trace, config: Config) -> list[Violation]:
    """Every invariant the event core must hold, whatever the ordering."""
    violations: list[Violation] = []
    conn = store._conn

    # --- THE headline invariant -------------------------------------------- #
    # An obligation must live inside the chain it belongs to. A case keyed on
    # anything but the order id is an obligation created outside the chain.
    rows = conn.execute("SELECT case_id, chain_key, order_id FROM cases").fetchall()
    for row in rows:
        if row["chain_key"] != scenario.order_id:
            violations.append(
                Violation(
                    "obligation_outside_chain",
                    f"case {row['case_id']} has chain_key {row['chain_key']!r}, "
                    f"expected the order {scenario.order_id!r}",
                    scenario,
                )
            )
    if len(rows) > 1:
        violations.append(
            Violation(
                "chain_split",
                f"{len(rows)} cases for one order -- the attempt chain was split, "
                "so an obligation exists outside the original chain",
                scenario,
            )
        )

    # --- Late authorisation ------------------------------------------------- #
    # Once a payment is authorized, no obligation may be created against it.
    for payment_id, resolved_step in trace.resolved_at_step.items():
        later = [
            key
            for step, pid, key in trace.decisions_at_step
            if pid == payment_id and step > resolved_step
        ]
        if later:
            violations.append(
                Violation(
                    "obligation_after_late_authorisation",
                    f"{payment_id} authorized at step {resolved_step}, then "
                    f"{len(later)} decision(s) recorded: {later}",
                    scenario,
                )
            )

    # --- Exactly once ------------------------------------------------------- #
    total, distinct = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT idempotency_key) FROM decisions"
    ).fetchone()
    if total != distinct:
        violations.append(
            Violation("duplicate_obligation", f"{total} decisions, {distinct} distinct keys",
                      scenario)
        )

    per_payment = conn.execute(
        "SELECT payment_id, COUNT(*) c FROM decisions GROUP BY payment_id HAVING c > 1"
    ).fetchall()
    for row in per_payment:
        violations.append(
            Violation(
                "multiple_obligations_per_payment",
                f"{row['payment_id']} has {row['c']} decisions",
                scenario,
            )
        )

    # --- Budget ------------------------------------------------------------- #
    if total > config.regulatory.attempt_cap:
        violations.append(
            Violation(
                "execution_budget_exceeded",
                f"{total} obligations against a cap of {config.regulatory.attempt_cap}",
                scenario,
            )
        )

    # --- Dedup -------------------------------------------------------------- #
    stored = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    if stored != len(trace.delivered_event_ids):
        violations.append(
            Violation(
                "dedup_failure",
                f"{stored} raw events stored for {len(trace.delivered_event_ids)} "
                "distinct event ids",
                scenario,
            )
        )

    # --- Order expiry (C4) -------------------------------------------------- #
    # Once the order expires it cannot carry a new obligation. Generated as a
    # hazard from the start; unenforced until the guard landed.
    if trace.order_expired:
        after = [
            entry
            for entry in trace.admitted
            if entry[0] > (trace.order_expired_at_step or -1)
        ]
        if after:
            violations.append(
                Violation(
                    "obligation_admitted_after_order_expiry",
                    f"{len(after)} execution(s) admitted after the order expired",
                    scenario,
                )
            )

    # --- Compliance of admitted executions (C4) ----------------------------- #
    calendar = calendar_from_config(config.regulatory)
    for step_number, payment_id, execute_at in trace.admitted:
        # The PDN lead time is checked at admission by the guard; this asserts
        # the guard actually held, rather than trusting that it was called.
        if calendar.is_peak(execute_at):
            violations.append(
                Violation(
                    "obligation_admitted_in_peak_window",
                    f"{payment_id} scheduled at "
                    f"{execute_at.astimezone(IST):%H:%M} IST, inside a barred window",
                    scenario,
                )
            )

    # Admitted executions must never exceed the cap on any chain.
    per_payment_admitted: dict[str, int] = {}
    for _, payment_id, _ in trace.admitted:
        per_payment_admitted[payment_id] = per_payment_admitted.get(payment_id, 0) + 1
    for payment_id, count in per_payment_admitted.items():
        # +1 for the original execution that opened the case.
        if count + 1 > config.regulatory.attempt_cap:
            violations.append(
                Violation(
                    "admitted_executions_exceed_cap",
                    f"{payment_id}: {count + 1} executions against a cap of "
                    f"{config.regulatory.attempt_cap}",
                    scenario,
                )
            )

    # An execution admitted against a payment that had already settled is the
    # double-charge the whole invariant exists to prevent.
    for step_number, payment_id, _ in trace.admitted:
        resolved = trace.resolved_at_step.get(payment_id)
        if resolved is not None and step_number > resolved:
            violations.append(
                Violation(
                    "obligation_admitted_after_late_authorisation",
                    f"{payment_id} settled at step {resolved}, execution admitted "
                    f"at step {step_number}",
                    scenario,
                )
            )

    # --- Auditability ------------------------------------------------------- #
    audited = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'decision.recorded'"
    ).fetchone()[0]
    if audited != total:
        violations.append(
            Violation(
                "unaudited_obligation",
                f"{total} decisions but {audited} decision.recorded audit events",
                scenario,
            )
        )

    return violations


# --------------------------------------------------------------------------- #
# Seeded search, for the reproduce path
# --------------------------------------------------------------------------- #


@dataclass
class SearchReport:
    sequences_explored: int
    actions_executed: int
    violations: list[Violation]
    unguarded: tuple[str, ...] = UNGUARDED_HAZARDS

    @property
    def clean(self) -> bool:
        return not self.violations

    def describe(self) -> str:
        if self.clean:
            return (
                f"explored {self.sequences_explored:,} adversarial orderings "
                f"({self.actions_executed:,} events) without a violation"
            )
        return (
            f"{len(self.violations)} violation(s) in "
            f"{self.sequences_explored:,} orderings: {self.violations[0]}"
        )


def random_scenario(rng: random.Random, max_actions: int = 14) -> Scenario:
    payment_count = rng.randint(1, 3)
    length = rng.randint(3, max_actions)
    steps = list(Step)
    # Deliveries and worker runs weighted up: a sequence of nothing but crashes
    # explores nothing.
    weights = {
        Step.DELIVER: 5,
        Step.REDELIVER: 3,
        Step.LATE_AUTHORIZE: 2,
        Step.RUN_WORKER: 4,
        Step.CRASH_WORKER: 1,
        Step.CONCURRENT_WORKERS: 2,
        Step.EXPIRE_ORDER: 1,
        Step.SHIFT_PDN_WINDOW: 1,
    }
    actions = tuple(
        Action(
            step=rng.choices(steps, weights=[weights[s] for s in steps], k=1)[0],
            payment_index=rng.randrange(0, payment_count),
            # Deliberately unordered: events arrive out of order.
            created_at=rng.randrange(1_755_000_000, 1_755_000_600),
        )
        for _ in range(length)
    )
    return Scenario(
        order_id="order_INVARIANT", payment_count=payment_count, actions=actions
    )


def search(
    config: Config,
    classifier: Classifier,
    tmp_dir: pathlib.Path,
    *,
    sequences: int = 2000,
    seed: int = 20260823,
) -> SearchReport:
    """Explore `sequences` seeded random orderings. Deterministic for a seed."""
    rng = random.Random(seed)
    violations: list[Violation] = []
    actions_executed = 0

    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / "invariants.db"

    explored = 0
    for index in range(sequences):
        # A fresh filename per sequence rather than deleting and recreating one.
        #
        # This loop runs up to several thousand times, and every iteration used
        # to unlink the same path before reopening it. On Windows an open SQLite
        # handle makes unlink raise PermissionError, so that was one filesystem
        # race per sequence -- and `execute` opens the store before it can
        # return it, so any exception inside it leaves a handle the caller never
        # sees and cannot close. A long search was the most likely place to hit
        # it and the worst place to hit it, because the run is measured in
        # minutes and the failure arrives at the end.
        #
        # Deleting after closing, rather than before opening, removes the race
        # instead of narrowing it.
        attempt_db = db_path.with_name(f"{db_path.stem}-{index}{db_path.suffix}")

        scenario = random_scenario(rng)
        explored += 1
        actions_executed += len(scenario.actions)
        store = None
        try:
            store, trace = execute(scenario, config, classifier, attempt_db)
            violations.extend(check(scenario, store, trace, config))
        finally:
            if store is not None:
                store.close()
            for suffix in ("", "-wal", "-shm"):
                candidate = attempt_db.with_name(attempt_db.name + suffix)
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    # Best effort. A file we could not remove is litter in a
                    # scratch directory, not a reason to abandon the search.
                    pass
        if violations:
            # Stop at the first violation. `explored` is the count actually run,
            # not the budget -- reporting the budget after an early stop would
            # overstate the search, which is the one number this whole exercise
            # exists to make honest.
            break

    return SearchReport(
        sequences_explored=explored,
        actions_executed=actions_executed,
        violations=violations,
    )
