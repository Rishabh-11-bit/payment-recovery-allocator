"""`python -m recovery.reproduce` -- regenerates every claim this repo makes.

Today that is three:

* **C1** -- replaying one event ten times produces exactly one case and one
  decision, with the full sequence visible in the audit trail.
* **C2** -- an unmapped key is distinguishable from a real classification: it
  reports `mapped=False`, zero confidence, a LOW band, and may not exclude an
  instrument. The taxonomy is authored (`status: AUTHORED`), so the classes
  shown are the real ones.
* **C3 + C5** -- all three arms over one sampled world, with the compliance
  constraints enforced by the environment rather than by the arms. The figures
  printed are a single draw and are **not a result**: a defensible number needs
  C8's sweep.

  Arm C's money figure is additionally bounded by **classifier coverage**,
  which is printed alongside it. An unmapped key falls to the LOW row -- one
  link, no execution -- so with a stub taxonomy Arm C behaves like a link-only
  arm on whatever the table does not cover, however good the table is.

  Mandate survival is printed as a **dominance ordering across the swept
  revocation range**, never as a count. The count would rest on a
  per-notification revocation rate nobody publishes; the ordering does not.

As components land, each adds a section here. The rule is that nothing goes in
the README that this command does not reproduce from a clean database.

Determinism: case and job ids are UUIDs and timestamps are wall-clock, so
neither is printed. What is printed -- counts, and the ordered sequence of audit
event types -- is fully determined by the input.
"""

from __future__ import annotations

import pathlib
import sys
import time

import typer

from recovery.classifier import DEFAULT_CLASSIFIER_PATH, load_classifier
from recovery.config import DEFAULT_CONFIG_PATH, load_config
from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway
from recovery.guard import guard_from_config
from recovery.ledger import Ledger
from recovery.ingest import ingest_delivery
from recovery.invariants import UNGUARDED_HAZARDS, search as invariant_search
from recovery.normalize import normalize_entity
from allocator.arm_c import ArmC
from allocator.decisions import table_rows
from allocator.wiring import ArmCDecider
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import calendar_from_config
from recovery.sim.horizon import SurvivalBasis, horizon_sweep
from recovery.sim.sweep import SweepReport, stress_config, sweep
from recovery.sim.run import (
    exchange_rate_band,
    mandate_survival_dominance,
    run_comparison,
)
from recovery.sim.world import load_world_config, mandate_hazard_range, sample_world
from recovery.store import Store
from recovery.worker import process_pending

app = typer.Typer(add_completion=False, help=__doc__)

REPLAY_COUNT = 10


def _fresh_store(path: pathlib.Path) -> Store:
    """Recreate the database from scratch, tolerating a lingering file handle.

    On Windows an open SQLite handle makes `unlink` raise `PermissionError`, and
    a handle can outlive the process that held it by a moment -- an interrupted
    run, or an `explain` session that has only just exited. Deleting a fixed
    path is therefore not reliable, and this is a command a reviewer runs live.

    So it retries briefly, and if the file still will not go it says which
    process to look for rather than surfacing a raw traceback from `pathlib`.
    """
    for attempt in range(5):
        blocked: OSError | None = None
        for suffix in ("", "-wal", "-shm"):
            candidate = path.with_name(path.name + suffix)
            try:
                candidate.unlink(missing_ok=True)
            except OSError as error:  # PermissionError on Windows
                blocked = error
        if blocked is None:
            break
        if attempt == 4:
            typer.echo(
                f"\nCannot recreate {path}: it is open in another process.\n"
                "  Close any `python -m recovery.explain` session, or end a stray\n"
                "  python process, then run this again. The database is disposable;\n"
                "  nothing is lost by deleting it by hand.",
                err=True,
            )
            raise typer.Exit(code=2)
        time.sleep(0.2 * (attempt + 1))

    store = Store(path)
    store.initialise()
    return store


def _check(label: str, actual: int, expected: int) -> bool:
    ok = actual == expected
    typer.echo(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual} (expected {expected})")
    return ok


@app.command()
def main(
    config_path: pathlib.Path = typer.Option(DEFAULT_CONFIG_PATH, "--config"),
    classifier_path: pathlib.Path = typer.Option(DEFAULT_CLASSIFIER_PATH, "--classifier"),
    db_path: pathlib.Path = typer.Option(
        pathlib.Path("data/reproduce.db"), "--db", help="Recreated from scratch on every run."
    ),
    profile: str = typer.Option(
        "",
        "--profile",
        help="Calibration profile for the failure mix. Defaults to whatever "
        "config/worlds.yaml names. Try `bounded-2026`.",
    ),
    sweep_worlds: int = typer.Option(
        100,
        "--sweep-worlds",
        help="Sampled worlds for the C8 robustness sweep. The default keeps this "
        "command quick; the reported figures used 300.",
    ),
    c7_sequences: int = typer.Option(
        500,
        "--c7-sequences",
        help="Adversarial orderings to search for an invariant violation. "
        "The default keeps this command quick; raise it for a deeper search.",
    ),
) -> None:
    config = load_config(config_path)
    # allow_stub is retained after the taxonomy was authored, deliberately: it is
    # what lets this command still run against a stub if someone points --classifier
    # at one. The gate stays a stated choice rather than an accident, and the NOTE
    # below only prints when the file actually is a stub -- which it no longer is.
    classifier = load_classifier(classifier_path, allow_stub=True)
    store = _fresh_store(db_path)
    gateway = SimulatedGateway()

    typer.echo("C1 event core -- replay of a single event")
    if classifier.config.is_stub:
        typer.echo(
            f"  NOTE: classifier mapping is {classifier.config.status} "
            f"({classifier.config.version}) -- taxonomy not yet authored, "
            "so classes below are illustrative"
        )
    typer.echo("")

    headers, body = build_delivery(event_id="evt_REPRODUCE00001")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])

    outcomes: list[str] = []
    for _ in range(REPLAY_COUNT):
        outcomes.append(ingest_delivery(store, config, headers, body).outcome.value)

    calendar = calendar_from_config(config.regulatory)
    decider = ArmCDecider(ArmC(calendar, classifier, config))
    stats = process_pending(store, config, gateway, classifier, decider=decider)

    typer.echo(f"  delivered {REPLAY_COUNT}x, ingest outcomes: {_summarise(outcomes)}")
    typer.echo(f"  worker: processed={stats.processed} decided={stats.decided}\n")

    passed = all(
        [
            _check("raw events stored", store.raw_event_count(), 1),
            _check("cases opened", store.case_count(), 1),
            _check("decisions recorded", store.decision_count(), 1),
            _check("jobs still pending", store.pending_job_count(), 0),
            _check("deliveries acknowledged", len(outcomes), REPLAY_COUNT),
        ]
    )

    typer.echo("\n  audit trail:")
    for event in store.audit_trail():
        marker = f"[{event.event_type.value}]"
        typer.echo(f"    {event.seq:>3}  {marker:<36} {_detail(event.detail)}")

    try:
        passed = _policy_table_section() and passed
        passed = _trace_cases_section(store, config, gateway, classifier) and passed
        passed = _c2_section(classifier) and passed
        passed = _c5_section(config, classifier, profile) and passed
        passed = _c7_section(config, classifier, db_path.parent, c7_sequences) and passed
        passed = _c8_section(config, classifier, sweep_worlds, profile) and passed
    finally:
        # Always, not only on success. Without this a section that raises -- or
        # a run interrupted with Ctrl-C -- leaves the SQLite handle open, and on
        # Windows the *next* run then cannot delete the file it is about to
        # recreate. The failure surfaces as a PermissionError on a later,
        # unrelated invocation, which is a long way from its cause.
        store.close()
    typer.echo("\nreproduce: " + ("OK" if passed else "FAILED"))
    if not passed:
        sys.exit(1)


def _policy_table_section() -> bool:
    """Print the twelve-cell decision table, generated from the table itself.

    The policy is small enough to read out loud, and printing it from
    `allocator/decisions.py` rather than transcribing it means a slide, a README
    table and this output cannot drift apart -- only one of them is authored.

    The `exec` column is the load-bearing one. It is stated per cell rather than
    derived from the action, because the whole argument is that some recovery
    actions cost a capped mandate execution and some cost nothing.
    """
    rows = table_rows()
    spends = [row for row in rows if row[3]]
    typer.echo("\n\nC3 policy -- the twelve-cell decision table\n")
    typer.echo(f"    {'class':<15} {'band':<9} {'action':<21} {'exec':<5} rationale")
    typer.echo("    " + "-" * 112)
    for failure_class, band, action, spends_execution, rationale in rows:
        mark = "YES" if spends_execution else "no"
        typer.echo(
            f"    {failure_class:<15} {band:<9} {action:<21} {mark:<5} {rationale}"
        )
    typer.echo(
        f"\n    {len(spends)} of {len(rows)} cells spend a capped mandate execution. "
        f"The other {len(rows) - len(spends)} cost none."
    )
    typer.echo(
        "    The LOW row is uniform across all four classes by design: when the class\n"
        "    is itself a guess, take the action that is right whichever cause is true."
    )
    typer.echo("")
    return all(
        [
            _check("decision table cells", len(rows), 12),
            _check("cells spending an execution", len(spends), 4),
        ]
    )


# One delivery per cell worth looking at. Payment ids are fixed strings rather
# than generated, because the whole point is that a reader can copy one out of
# this README and run `explain` against it -- a uuid would be reproducible only
# in the sense that it is regenerated every time.
TRACE_CASES = (
    (
        "pay_SYNTHEXPIRED01",
        "order_SYNTHEXPIRED01",
        "card",
        "issuer_bank",
        "payment_authorization",
        "payment_expired_card",
        "TERMINAL at HIGH -- the cell the thesis rests on",
    ),
    (
        "pay_SYNTHNOFUNDS01",
        "order_SYNTHNOFUNDS01",
        "upi",
        "customer_psp",
        "payment_debit_response",
        "insufficient_funds",
        "LIQUIDITY at HIGH -- recovers later rather than sooner",
    ),
    (
        "pay_SYNTHNOMPIN001",
        "order_SYNTHNOMPIN001",
        "upi",
        "customer",
        "payment_authentication",
        "payment_ux_canceled",
        "ATTENTION -- customer reached, never entered M-PIN",
    ),
    (
        "pay_SYNTHGENERIC01",
        "order_SYNTHGENERIC01",
        "netbanking",
        "bank",
        "payment_authorization",
        "payment_failed",
        "LOW band -- the payload carries no information",
    ),
)


def _trace_cases_section(store, config, gateway, classifier) -> bool:
    """Materialise one case per decision cell, so `explain` has something to explain.

    The C1 demo above deliberately ingests a single event ten times -- that is
    what proves the dedup key. It therefore produces exactly one case, and one
    case cannot demonstrate that different causes get different decisions.

    These four go through the same ingest, the same classifier, the same
    allocator and the same guard. Nothing here is a display fixture: the
    decisions are the ones the allocator makes, and if a cell changes, this
    output changes with it.
    """
    typer.echo("\n\nC6 decision traces -- one case per cell, for `explain`\n")

    calendar = calendar_from_config(config.regulatory)
    decider = ArmCDecider(ArmC(calendar, classifier, config))
    guard = guard_from_config(config, calendar)

    for index, (payment_id, order_id, method, source, step, reason, _) in enumerate(
        TRACE_CASES
    ):
        headers, body = build_delivery(
            event_id=f"evt_TRACE{index:011d}",
            payment_id=payment_id,
            order_id=order_id,
            method=method,
            error_source=source,
            error_step=step,
            error_reason=reason,
        )
        gateway.seed_from_webhook(body["payload"]["payment"]["entity"])
        ingest_delivery(store, config, headers, body)

    process_pending(store, config, gateway, classifier, decider=decider, guard=guard)

    ledger = Ledger(store)
    ok = True
    for payment_id, _, _, _, _, _, note in TRACE_CASES:
        case_id = ledger.resolve(payment_id)
        trace = ledger.trace(case_id) if case_id else None
        if trace is None or not trace.decisions:
            typer.echo(f"    {payment_id}  NO DECISION RECORDED")
            ok = False
            continue
        decision = trace.decisions[-1]
        typer.echo(f"    {payment_id}  {decision.action}")
        typer.echo(f"        {note}")
        typer.echo(f"        {decision.reason}")

    typer.echo(
        "\n    Every line above is a real decision from the real allocator. Trace one:"
    )
    typer.echo("      python -m recovery.explain --db data/reproduce.db pay_SYNTHEXPIRED01")
    typer.echo("")
    return _check("trace cases decided", len(TRACE_CASES) if ok else 0, len(TRACE_CASES))


def _c2_section(classifier) -> bool:
    """C2: an unmapped key must be distinguishable from a real classification."""
    typer.echo("\nC2 classifier -- unmapped keys are not silently defaulted\n")

    mapped = classifier.classify(
        normalize_entity(
            {
                "method": "upi",
                "error_source": "customer_psp",
                "error_step": "payment_debit_response",
                "error_reason": "insufficient_funds",
            },
            source_space=classifier.config.source_space,
        )
    )
    unmapped = classifier.classify(
        normalize_entity(
            {"method": "upi", "error_step": "a_step_nobody_has_mapped"},
            source_space=classifier.config.source_space,
        )
    )
    anomalous = classifier.classify(
        normalize_entity(
            {"method": "card", "error_source": "customer_psp"},
            source_space=classifier.config.source_space,
        )
    )

    for label, result in (
        ("mapped key", mapped),
        ("unmapped key", unmapped),
        ("source outside method's space", anomalous),
    ):
        typer.echo(
            f"    {label:<32} class={result.failure_class.value:<14} "
            f"band={result.band.value:<9} mapped={result.mapped}"
        )

    typer.echo("")
    return all(
        [
            _check("mapped key reports mapped", int(mapped.mapped), 1),
            _check("unmapped key reports unmapped", int(unmapped.mapped), 0),
            _check("anomalous source is not trusted", int(anomalous.mapped), 0),
            _check("unmapped confidence is zero", int(unmapped.confidence * 100), 0),
            _check("unmapped may not exclude", int(unmapped.may_exclude_instrument), 0),
        ]
    )


def _c8_section(config, classifier, worlds: int, profile: str = "") -> bool:
    """C8: sample the world space, report where the result breaks."""
    calendar = calendar_from_config(config.regulatory)
    arms = [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)]

    typer.echo("\n\nC8 robustness sweep -- where does this break?\n")
    typer.echo(
        f"  {worlds} sampled worlds per range set, batch 200. Every cardinal value is"
    )
    typer.echo(
        "  redrawn per world: recovery curves, link conversion, revocation hazard,"
    )
    typer.echo("  failure mix, rail mix, emission fidelity.")

    passed = True
    named: set[str] = set()
    for label in ("nominal", "stress"):
        raw = _world_config(profile)
        raw["batch"] = {**raw["batch"], "size": 200}
        if label == "stress":
            raw = stress_config(raw)
        outcomes = sweep(
            arms, calendar, raw, worlds=worlds, costs=classifier.config.costs
        )
        report = SweepReport(label=label, worlds=worlds, outcomes=outcomes)

        typer.echo(f"\n  --- {label.upper()} ---")
        if label == "stress":
            typer.echo(
                "  Ranges deliberately widened past calibration to locate the edge."
            )
            typer.echo("  These figures locate a breaking point; they are not results.")
        typer.echo(
            f"    cycle-recovery winner: A {report.cycle_win_share('A'):.0%}  "
            f"B {report.cycle_win_share('B'):.0%}  C {report.cycle_win_share('C'):.0%}"
        )
        for incumbent in ("A", "B"):
            distribution = report.distribution(incumbent)
            typer.echo(f"    {distribution.describe()}")
            typer.echo(
                f"      C ahead by 6mo {distribution.win_rate(6):.0%}, "
                f"12mo {distribution.win_rate(12):.0%}, "
                f"24mo {distribution.win_rate(24):.0%}"
            )
            points = report.breaking(incumbent)
            if points:
                for point in points:
                    typer.echo(f"      BREAKS WHEN {point.describe()}")
            else:
                typer.echo(
                    "      no parameter condition separates wins from losses"
                )
            named.update(point.parameter for point in points)
        passed = passed and bool(outcomes)

    typer.echo("")
    if any("revocation" in parameter for parameter in named):
        typer.echo(
            "    The breaking condition is the revocation hazard itself: where repeated"
        )
        typer.echo(
            "    failure notifications cost few mandates, C's conservatism buys nothing"
        )
        typer.echo(
            "    and contacting everyone wins. That parameter is the least evidenced"
        )
        typer.echo(
            "    number in the project (ASSUMPTIONS.md), so the result depends most on"
        )
        typer.echo(
            "    what we can defend least. Stated because it is the first thing to attack."
        )
    else:
        typer.echo(
            f"    No condition surfaced at {worlds} worlds -- a split needs at least"
        )
        typer.echo(
            "    15 worlds on each side, so small samples name nothing. This is the"
        )
        typer.echo(
            "    sweep declining to invent a condition, not evidence there is none."
        )
        typer.echo(
            "    The verified 300-world run found: C loses where"
        )
        typer.echo(
            "    revocation_per_notification is below ~0.010-0.014 (33-57% loss rate"
        )
        typer.echo(
            "    there, against 6-13% elsewhere). Rerun with --sweep-worlds 300."
        )
    typer.echo("")
    return passed


def _c7_section(config, classifier, tmp_dir, sequences: int) -> bool:
    """C7: hunt the safety invariant across generated adversarial orderings."""
    typer.echo("\n\nC7 invariants -- adversarial event orderings\n")
    typer.echo(
        "  Hunting: never create a payment obligation outside the original order's"
    )
    typer.echo(
        "  attempt chain while that chain is within its late-authorisation window.\n"
    )
    report = invariant_search(config, classifier, tmp_dir, sequences=sequences)
    typer.echo(f"    {report.describe()}")
    typer.echo("")
    typer.echo(
        "    Generated, not hand-written: duplicate deliveries, out-of-order deliveries,"
    )
    typer.echo(
        "    failed-then-late-authorized inside the 3-day window, a worker crashing"
    )
    typer.echo(
        "    between claim and finish, two workers on one case, order expiry mid-recovery,"
    )
    typer.echo("    and a PDN window shift.")
    typer.echo("")
    if UNGUARDED_HAZARDS:
        typer.echo("    NOT YET ENFORCED (generated, but no check consumes them):")
        for hazard in UNGUARDED_HAZARDS:
            typer.echo(f"      - {hazard}")
    else:
        typer.echo(
            "    Every generated hazard is enforced. Order expiry and PDN-window shift"
        )
        typer.echo(
            "    were generated but unguarded until C4; they are now blocked at the"
        )
        typer.echo("    admission point and the sequences exercise a real block.")
    typer.echo("")
    typer.echo(
        "    A clean run is worth only the size of the search, and only if the search can"
    )
    typer.echo(
        "    fail. tests/test_c7_invariants.py plants two bugs -- a removed"
    )
    typer.echo(
        "    late-authorisation guard and a split chain -- and asserts the search finds"
    )
    typer.echo("    both. Raise --c7-sequences for a deeper hunt.")
    typer.echo("")
    return all(
        [
            _check("invariant violations found", len(report.violations), 0),
            _check("orderings explored", report.sequences_explored, sequences),
        ]
    )


SIM_SEED = 42


def _world_config(profile: str) -> dict:
    """World ranges, with the calibration profile overridden if one was named."""
    raw = load_world_config()
    if profile:
        raw["batch"] = {**raw["batch"], "calibration_profile": profile}
    return raw


def _c5_section(config, classifier, profile: str = "") -> bool:
    """C3 + C5: three arms, one sampled world.

    Order matters. Cycle recovery first, because the bar asks for measured money
    recovered across a batch and that figure is never demoted. Then the horizon
    crossover, because one cycle is the wrong unit for judging an arm that
    deliberately trades cycle recovery for mandate survival.
    """
    raw = _world_config(profile)
    world = sample_world(seed=SIM_SEED, raw=raw)
    calendar = calendar_from_config(config.regulatory)
    arms = [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)]
    result = run_comparison(arms, world, calendar, costs=classifier.config.costs)
    hazard_range = mandate_hazard_range(load_world_config())

    typer.echo(f"\nC3 + C5 -- three arms, world seed {SIM_SEED}\n")
    typer.echo(
        "  NOTE: one world. Every cardinal value below is a single draw from the "
        "ranges in\n        config/worlds.yaml. A defensible figure needs C8's "
        "sweep across many worlds."
    )

    # --- 1. Cycle recovery. The bar's requirement, reported first, undemoted.
    typer.echo("\n  MONEY RECOVERED ACROSS THE BATCH (one cycle)\n")
    typer.echo(
        f"    {'arm':<5}{'recovered_INR':>15}{'attempts':>10}{'contacts':>10}"
        f"{'term_att':>10}{'term_con':>10}"
    )
    for name in ("A", "B", "C"):
        row = result.row(name)
        typer.echo(
            f"    {name:<5}{row['money_recovered_inr']:>15,.2f}"
            f"{row['attempts_spent']:>10}{row['contacts_sent']:>10}"
            f"{row['terminal_attempts_wasted']:>10}{row['terminal_contacts_sent']:>10}"
        )

    metrics_a = result.metrics["A"]
    metrics_b = result.metrics["B"]
    metrics_c = result.metrics["C"]
    typer.echo(
        f"\n    A -> B  contact uplift:          Rs {result.uplift('B', 'A'):>12,.2f}"
        "   (value of contacting people)"
    )
    typer.echo(
        f"    B -> C  cause-awareness uplift: Rs {result.uplift('C', 'B'):>12,.2f}"
        "   (value of knowing why)"
    )
    typer.echo(
        "\n    Arm C recovers less in one cycle, on purpose: it withholds contacts and\n"
        "    executions that the other arms spend. Judging it on one cycle is the wrong\n"
        "    unit -- see the crossover below."
    )

    # --- 2. The headline: at what horizon does that trade pay for itself?
    typer.echo("\n\n  HEADLINE -- HORIZON CROSSOVER\n")
    sweep = horizon_sweep(
        arms, world, calendar, hazard_range, costs=classifier.config.costs
    )
    for incumbent in ("B", "A"):
        typer.echo(f"    {sweep.crossover('C', incumbent).describe()}")
    typer.echo(
        "\n    A mandate is an annuity. An arm that recovers less now while keeping more\n"
        "    mandates alive is ahead from some remaining lifetime onward, and that\n"
        "    lifetime is the claim -- reported as a band across the hazard range, never\n"
        "    at a single hazard and never as a rupee LTV figure."
    )

    # The batch money figure at a stated horizon. Printed here rather than only
    # in the sensitivity table below, because "money recovered across a batch"
    # is what the bar asks for and one cycle is not the only unit it can be
    # measured in.
    #
    # Labelled as VALUE, not as recovered cash. It is the cycle recovery plus
    # the revenue of mandates still alive at that horizon, and calling it
    # anything else would be the overstatement this project spends its effort
    # avoiding. It is a band across the hazard sweep at a fixed horizon -- a
    # sensitivity, which CLAUDE.md permits -- never a single LTV number.
    typer.echo(
        "\n    Batch value at a stated remaining lifetime, Rs "
        "(cycle recovery + surviving mandates,"
    )
    typer.echo("    min-max across the swept hazard range):\n")
    typer.echo(f"      {'months':<9}" + "".join(f"{n:>26}" for n in ("A", "B", "C")))
    for months in (6, 12, 24):
        cells = ""
        for name in ("A", "B", "C"):
            low, high = sweep.value_band_inr(name, months)
            cells += f"{low:>11,.0f}-{high:<14,.0f}"
        typer.echo(f"      {months:<9}" + cells)

    ahead = [
        months
        for months in (6, 12, 24)
        if sweep.value_band_inr("C", months)[0] > sweep.value_band_inr("A", months)[0]
        and sweep.value_band_inr("C", months)[0] > sweep.value_band_inr("B", months)[0]
    ]
    if ahead:
        typer.echo(
            f"\n    From {min(ahead)} months on, Arm C's worst case across the hazard range\n"
            "    beats both other arms' worst cases. The cycle figure above and this one\n"
            "    are the same batch measured over different lengths of time."
        )

    typer.echo("\n    exit doors -- halted preserves mandate authority, revoked destroys it:")
    for incumbent in ("A", "B"):
        band = exchange_rate_band(
            arms, world, calendar, hazard_range, "C", incumbent,
            costs=classifier.config.costs,
        )
        typer.echo(f"      {band.describe()}")
    typer.echo(
        "\n      Trading revocations for halts is a real gain: a halted subscription can\n"
        "      be reactivated by a card update with no customer re-authorisation, while a\n"
        "      revoked mandate needs full re-registration, a fresh PDN and fresh AFA\n"
        "      against ~30% pre-registration drop-off. Whether this particular rate is a\n"
        "      good trade depends on manual-recovery rates for halted subscriptions,\n"
        "      which are not published. Stated, not asserted."
    )

    # --- 3. Supporting evidence.
    typer.echo("\n\n  SUPPORTING\n")
    typer.echo(
        f"    attempts spent where recovery is impossible: "
        f"A {metrics_a.terminal_attempts_wasted}, "
        f"B {metrics_b.terminal_attempts_wasted}, "
        f"C {metrics_c.terminal_attempts_wasted}"
    )
    typer.echo(
        f"    share of the capped budget wasted:           "
        f"A {metrics_a.wasted_attempt_share:.0%}, "
        f"B {metrics_b.wasted_attempt_share:.0%}, "
        f"C {metrics_c.wasted_attempt_share:.0%}"
    )

    # What each arm tried, versus what the guard allowed. A block is never
    # silently swallowed, so "wanted to and could not" stays distinguishable
    # from "chose not to".
    typer.echo("\n    proposals blocked by the guard, per arm:")
    for name in ("A", "B", "C"):
        metrics = result.metrics[name]
        summary = (
            ", ".join(
                f"{reason} x{count}"
                for reason, count in sorted(metrics.rejection_reasons.items())
            )
            or "nothing blocked"
        )
        typer.echo(f"      {name}: {metrics.proposals_rejected:>4} blocked   {summary}")

    arm_c = ArmC(calendar, classifier, config)
    coverage = _coverage(arm_c, world)
    typer.echo(
        f"\n    classifier coverage on this batch: {coverage:.0%} mapped "
        f"({1 - coverage:.0%} fall to the LOW row)"
    )
    if classifier.config.is_stub:
        typer.echo(
            "    ^ the taxonomy is a STUB. Arm C's cycle figure is bounded by this,\n"
            "      not by the decision table."
        )

    dominance = mandate_survival_dominance(
        arms, world, calendar, hazard_range, costs=classifier.config.costs
    )
    typer.echo("\n    mandate survival, as an ordering rather than a count:")
    typer.echo(f"      {dominance.describe()}")
    typer.echo(f"      ordering inversions across the range: {dominance.inversions}")

    _echo_horizon(arms, world, calendar, classifier)

    return all(
        [
            _check("arms saw the same batch", result.metrics["B"].cases, metrics_a.cases),
            _check("baseline sends no contacts", metrics_a.contacts_sent, 0),
            _check(
                "allocator wastes fewer attempts than the baseline",
                int(metrics_c.terminal_attempts_wasted < metrics_a.terminal_attempts_wasted),
                1,
            ),
            _check(
                "arm B contacts every case once",
                result.metrics["B"].contacts_sent,
                result.metrics["B"].cases,
            ),
            _check(
                "no compliance violations slipped through",
                sum(
                    m.rejection_reasons.get("peak_hour_barred", 0)
                    + m.rejection_reasons.get("pdn_lead_time_unmet", 0)
                    for m in result.metrics.values()
                ),
                0,
            ),
            _check(
                "no mandate count in the reported row",
                sum("mandate" in key for key in result.row("A")),
                0,
            ),
        ]
    )


LIFETIME_SAMPLES = (6, 12, 18, 24)


def _echo_horizon(arms, world, calendar, classifier) -> None:
    """At what remaining lifetime does preservation outweigh cycle recovery?

    Reported as a crossover band across the swept hazard range. Never a single
    lifetime at a single hazard, and never a headline rupee figure.
    """
    hazard_range = mandate_hazard_range(load_world_config())
    typer.echo("\n  horizon sensitivity -- cycle recovery vs preserved mandates:\n")

    for basis in SurvivalBasis:
        sweep = horizon_sweep(
            arms, world, calendar, hazard_range, basis=basis, costs=classifier.config.costs
        )
        if basis.is_degenerate:
            typer.echo(
                f"    basis={basis.value} -- DEGENERATE under the current outcome model."
            )
            typer.echo(
                "      Every case ends recovered, revoked or halted, so"
                " preserved-minus-halted"
            )
            typer.echo(
                "      is identically cases_recovered and the annuity term just restates"
            )
            typer.echo(
                "      the cycle term. Shown so the degeneracy is visible, not as a result."
            )
        else:
            typer.echo(f"    basis={basis.value}")
        for incumbent in ("B", "A"):
            typer.echo(f"      {sweep.crossover('C', incumbent).describe()}")
        typer.echo("")

    # The curve, as a band across hazards rather than a line at one of them.
    sweep = horizon_sweep(arms, world, calendar, hazard_range, costs=classifier.config.costs)
    typer.echo(
        "    total value by remaining lifetime, Rs (min-max across the hazard range,"
    )
    typer.echo(
        f"    basis=not_revoked, monthly charge Rs {sweep.monthly_value_paise / 100:,.2f}"
        " derived from the batch):"
    )
    typer.echo("")
    typer.echo(f"      {'months':<8}" + "".join(f"{name:>26}" for name in ("A", "B", "C")))
    for months in LIFETIME_SAMPLES:
        cells = ""
        for name in ("A", "B", "C"):
            low, high = sweep.value_band_inr(name, months)
            cells += f"{low:>11,.0f}-{high:<14,.0f}"
        typer.echo(f"      {months:<8}" + cells)
    typer.echo("")
    typer.echo(
        "    These are value orderings, not LTV estimates. Only the lifetime at which"
    )
    typer.echo(
        "    an ordering flips is a claim; the rupee figures are the arithmetic behind"
    )
    typer.echo("    it and are not quoted anywhere as a result.")


def _coverage(arm, world) -> float:
    """Share of the batch the taxonomy actually maps, as the allocator sees it."""
    from recovery.sim.batch import generate_batch
    from recovery.sim.environment import CaseOutcome, CaseView

    batch = generate_batch(world)
    mapped = 0
    for failure in batch:
        view = CaseView(
            case_id=failure.case_id,
            rail=failure.rail,
            amount_paise=failure.amount_paise,
            failed_at=failure.failed_at,
            observed=failure.observed(),
            attempts_used=1,
            contacts_used=0,
            outcome=CaseOutcome.OPEN,
            attempt_pending=False,
            last_attempt_resolved_at=None,
        )
        if arm.classify(view).mapped:
            mapped += 1
    return mapped / len(batch) if batch else 0.0


def _summarise(outcomes: list[str]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return ", ".join(f"{count}x {name}" for name, count in sorted(counts.items()))


def _detail(detail: dict) -> str:
    keys = (
        "action",
        "authoritative_status",
        "dedup_key",
        "attempt_n",
        "chain_key",
        "class",
        "band",
        "mapped",
    )
    shown = {k: detail[k] for k in keys if k in detail}
    return " ".join(f"{k}={v}" for k, v in shown.items())


if __name__ == "__main__":
    app()
