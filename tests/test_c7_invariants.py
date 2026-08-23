"""C7 -- property-based invariant tests over adversarial event orderings.

The invariant being hunted:

> Never create a payment obligation outside the original order's attempt chain
> while that chain is within its late-authorisation window.

Hypothesis generates the orderings; it is not a hand-written case list. When a
violation exists, Hypothesis also shrinks it to the shortest sequence that still
breaks the invariant, which is the difference between "something is wrong" and
"here is the four-event sequence that does it".

**The mutation control is the load-bearing test in this file.**
`test_search_finds_the_bug_when_the_late_auth_guard_is_removed` disables the
late-authorisation check and asserts the search then *fails*. Without it, a
clean run proves only that the assertions are unreachable.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from recovery.invariants import (
    UNGUARDED_HAZARDS,
    Action,
    Scenario,
    Step,
    check,
    execute,
    random_scenario,
    search,
)
from recovery.models import PaymentStatus

# Raised above the default 100, but kept to a size the suite can carry on every
# run. The large search -- the one the "N thousand orderings" claim rests on --
# runs in `python -m recovery.reproduce`, which is where the number is reported.
# Override here with C7_EXAMPLES to search harder locally.
EXAMPLES = int(os.environ.get("C7_EXAMPLES", "400"))

SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

# How many examples Hypothesis actually ran, for the reported total.
explored: dict[str, int] = {"sequences": 0, "actions": 0}


actions = st.builds(
    Action,
    step=st.sampled_from(list(Step)),
    payment_index=st.integers(min_value=0, max_value=2),
    # Deliberately unordered and overlapping: events arrive out of order.
    created_at=st.integers(min_value=1_755_000_000, max_value=1_755_000_600),
)

scenarios = st.builds(
    Scenario,
    order_id=st.just("order_INVARIANT"),
    payment_count=st.integers(min_value=1, max_value=3),
    actions=st.lists(actions, min_size=1, max_size=16).map(tuple),
)


def run(scenario: Scenario, config, classifier, tmp_path: pathlib.Path) -> list:
    db_path = tmp_path / "hypothesis.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            candidate.unlink()
    store, trace = execute(scenario, config, classifier, db_path)
    try:
        return check(scenario, store, trace, config)
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# The hunt
# --------------------------------------------------------------------------- #


@SETTINGS
@given(scenario=scenarios)
def test_no_obligation_outside_the_chain(scenario, config, classifier, tmp_path):
    """The headline invariant, over generated adversarial orderings."""
    explored["sequences"] += 1
    explored["actions"] += len(scenario.actions)

    violations = run(scenario, config, classifier, tmp_path)
    assert not violations, "\n".join(str(v) for v in violations)


def test_report_how_many_orderings_were_explored(config, classifier, tmp_path):
    """"I could not break it" is meaningless without the size of the search.

    Runs last by name ordering within the module is not guaranteed, so this
    reports the seeded search's own total rather than depending on the
    Hypothesis counter having been filled.
    """
    report = search(config, classifier, tmp_path, sequences=200)
    assert report.clean, report.describe()
    assert report.sequences_explored == 200
    assert report.actions_executed > report.sequences_explored
    print(f"\n  seeded search: {report.describe()}")
    print(f"  hypothesis:    {explored['sequences']:,} orderings so far this session")
    print(f"  NOT YET GUARDED (generated, but no check enforces them):")
    for hazard in UNGUARDED_HAZARDS:
        print(f"    - {hazard}")


# --------------------------------------------------------------------------- #
# Mutation control -- without this, a clean run proves nothing
# --------------------------------------------------------------------------- #


def test_search_reports_the_count_actually_run_not_the_budget(
    config, classifier, tmp_path, monkeypatch
):
    """An early stop must not report the full budget as explored."""
    monkeypatch.setattr(
        PaymentStatus, "is_resolved", property(lambda self: False), raising=False
    )
    report = search(config, classifier, tmp_path, sequences=5000)
    assert not report.clean
    assert report.sequences_explored < 5000, "reported the budget, not the search"


def test_search_finds_the_bug_when_the_late_auth_guard_is_removed(
    config, classifier, tmp_path, monkeypatch
):
    """Disable the late-authorisation check; the search must then fail.

    This is what licenses the claim. A search that cannot find a planted bug is
    not evidence that no bug exists -- it is evidence that nothing was checked.
    """
    monkeypatch.setattr(
        PaymentStatus, "is_resolved", property(lambda self: False), raising=False
    )
    report = search(config, classifier, tmp_path, sequences=400)

    assert not report.clean, "the planted bug was not found -- the search is blind"
    assert any(
        v.name == "obligation_after_late_authorisation" for v in report.violations
    ), [v.name for v in report.violations]


def test_search_finds_a_split_chain(config, classifier, tmp_path, monkeypatch):
    """Plant the other headline failure: an obligation outside the chain."""
    import recovery.worker as worker_module

    original = worker_module.normalize_snapshot

    def blind_to_order(snapshot, **kwargs):
        return original(snapshot, **kwargs)

    # Force every payment onto its own chain by stripping the order id.
    from recovery.gateway import SimulatedGateway

    fetch = SimulatedGateway.fetch_payment

    def orderless(self, payment_id):
        snapshot = fetch(self, payment_id)
        return snapshot.model_copy(update={"order_id": None})

    monkeypatch.setattr(SimulatedGateway, "fetch_payment", orderless)
    monkeypatch.setattr(worker_module, "normalize_snapshot", blind_to_order)

    report = search(config, classifier, tmp_path, sequences=400)
    assert not report.clean, "a split chain went undetected"
    assert any(
        v.name in {"obligation_outside_chain", "chain_split"} for v in report.violations
    ), [v.name for v in report.violations]


# --------------------------------------------------------------------------- #
# Named hazards, each pinned so a regression names itself
# --------------------------------------------------------------------------- #


def scenario_of(*steps: Step, payment_count: int = 1) -> Scenario:
    return Scenario(
        order_id="order_INVARIANT",
        payment_count=payment_count,
        actions=tuple(
            Action(step=step, payment_index=0, created_at=1_755_000_000 + index)
            for index, step in enumerate(steps)
        ),
    )


def test_duplicate_delivery_creates_one_obligation(config, classifier, tmp_path):
    scenario = scenario_of(
        Step.DELIVER, Step.REDELIVER, Step.REDELIVER, Step.RUN_WORKER
    )
    assert not run(scenario, config, classifier, tmp_path)


def test_late_authorisation_after_failure_creates_none(config, classifier, tmp_path):
    """The documented 3-day window, in the order that matters."""
    scenario = scenario_of(Step.DELIVER, Step.LATE_AUTHORIZE, Step.RUN_WORKER)
    assert not run(scenario, config, classifier, tmp_path)


def test_worker_crash_then_restart(config, classifier, tmp_path):
    scenario = scenario_of(
        Step.DELIVER, Step.CRASH_WORKER, Step.RUN_WORKER, Step.RUN_WORKER
    )
    assert not run(scenario, config, classifier, tmp_path)


def test_concurrent_workers_on_one_case(config, classifier, tmp_path):
    scenario = scenario_of(Step.DELIVER, Step.CONCURRENT_WORKERS, Step.RUN_WORKER)
    assert not run(scenario, config, classifier, tmp_path)


def test_out_of_order_deliveries_across_payments(config, classifier, tmp_path):
    scenario = Scenario(
        order_id="order_INVARIANT",
        payment_count=3,
        actions=(
            Action(Step.DELIVER, 2, 1_755_000_500),
            Action(Step.DELIVER, 0, 1_755_000_100),
            Action(Step.RUN_WORKER),
            Action(Step.DELIVER, 1, 1_755_000_300),
            Action(Step.RUN_WORKER),
        ),
    )
    assert not run(scenario, config, classifier, tmp_path)


def test_order_expiry_mid_recovery(config, classifier, tmp_path):
    scenario = scenario_of(
        Step.DELIVER, Step.RUN_WORKER, Step.EXPIRE_ORDER, Step.DELIVER, Step.RUN_WORKER
    )
    assert not run(scenario, config, classifier, tmp_path)


def test_pdn_window_shift(config, classifier, tmp_path):
    scenario = scenario_of(
        Step.DELIVER, Step.SHIFT_PDN_WINDOW, Step.RUN_WORKER
    )
    assert not run(scenario, config, classifier, tmp_path)


# --------------------------------------------------------------------------- #
# Liveness -- safe is not the same as working
# --------------------------------------------------------------------------- #


def test_crashed_job_is_reclaimed_not_dropped(config, classifier, tmp_path):
    """Dropping a job breaks no invariant and still loses the recovery.

    Found by the C7 search: `claim_timeout_seconds` was in config and ignored by
    the store, so a worker dying between claim and finish abandoned the event
    permanently.
    """
    from recovery.fixtures import build_delivery
    from recovery.gateway import SimulatedGateway
    from recovery.ingest import ingest_delivery
    from recovery.store import Store
    from recovery.worker import process_pending

    store = Store(tmp_path / "reclaim.db")
    store.initialise()
    gateway = SimulatedGateway()
    headers, body = build_delivery(event_id="evt_crash", payment_id="pay_1")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
    ingest_delivery(store, config, headers, body)

    store.claim_jobs(50)  # crash: claimed, never finished
    assert store.claimed_job_count() == 1
    assert store.decision_count() == 0

    impatient = config.model_copy(
        update={"worker": config.worker.model_copy(update={"claim_timeout_seconds": 0.001})}
    )
    process_pending(store, impatient, gateway, classifier)

    assert store.claimed_job_count() == 0
    assert store.decision_count() == 1
    store.close()


def test_reclaim_gives_up_after_max_attempts(config, classifier, tmp_path):
    """A job that kills every worker must fail visibly, not loop forever."""
    from recovery.fixtures import build_delivery
    from recovery.ingest import ingest_delivery
    from recovery.store import Store

    store = Store(tmp_path / "giveup.db")
    store.initialise()
    headers, body = build_delivery(event_id="evt_poison", payment_id="pay_1")
    ingest_delivery(store, config, headers, body)

    for _ in range(config.worker.max_attempts_per_job + 2):
        store.claim_jobs(
            50,
            claim_timeout_seconds=0.0,
            max_attempts=config.worker.max_attempts_per_job,
        )

    states = [
        row["state"] for row in store._conn.execute("SELECT state FROM jobs").fetchall()
    ]
    assert states == ["failed"]
    store.close()


# --------------------------------------------------------------------------- #
# The checker itself
# --------------------------------------------------------------------------- #


def test_random_scenarios_are_varied():
    """A generator that emits one shape explores one shape."""
    import random

    rng = random.Random(1)
    shapes = {
        tuple(a.step for a in random_scenario(rng).actions) for _ in range(200)
    }
    assert len(shapes) > 150


def test_no_hazard_is_left_unguarded():
    """Order expiry and PDN shift were generated but unenforced until C4."""
    assert UNGUARDED_HAZARDS == ()


# --------------------------------------------------------------------------- #
# C4 hazards -- generated from the start, enforced since the guard landed
# --------------------------------------------------------------------------- #


def test_generated_sequences_actually_exercise_the_guard(config, classifier, tmp_path):
    """A hazard that never fires a block is a hazard in name only."""
    import collections
    import random

    from recovery.invariants import execute, random_scenario

    rng = random.Random(11)
    reasons: collections.Counter = collections.Counter()
    admitted = 0
    for index in range(120):
        scenario = random_scenario(rng)
        store, trace = execute(
            scenario, config, classifier, f"file:t7_{index}?mode=memory&cache=shared"
        )
        reasons.update(trace.blocks)
        admitted += len(trace.admitted)
        store.close()

    assert admitted, "no execution was ever admitted -- the invariants are vacuous"
    assert "order_expired" in reasons, "order expiry never blocked anything"
    assert "pdn_lead_time_unmet" in reasons, "the PDN window never blocked anything"
    assert "peak_hour_barred" in reasons


def test_search_finds_an_obligation_admitted_after_order_expiry(
    config, classifier, tmp_path, monkeypatch
):
    """Mutation control for the order check."""
    from recovery.guard import ALLOWED, Guard

    monkeypatch.setattr(Guard, "_order", lambda self, request: ALLOWED)
    report = search(config, classifier, tmp_path, sequences=400)

    assert not report.clean, "expired-order admission went undetected"
    assert any(
        v.name == "obligation_admitted_after_order_expiry" for v in report.violations
    ), [v.name for v in report.violations]


def test_search_finds_an_execution_admitted_in_a_barred_window(
    config, classifier, tmp_path, monkeypatch
):
    """Mutation control for the timing checks."""
    from recovery.guard import ALLOWED, Guard

    monkeypatch.setattr(Guard, "_execution", lambda self, request: ALLOWED)
    report = search(config, classifier, tmp_path, sequences=400)

    assert not report.clean, "peak-window admission went undetected"
    assert any(
        v.name
        in {
            "obligation_admitted_in_peak_window",
            "admitted_executions_exceed_cap",
            "obligation_admitted_after_late_authorisation",
        }
        for v in report.violations
    ), [v.name for v in report.violations]
