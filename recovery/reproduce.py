"""`python -m recovery.reproduce` -- regenerates every claim this repo makes.

Today that is three:

* **C1** -- replaying one event ten times produces exactly one case and one
  decision, with the full sequence visible in the audit trail.
* **C2** -- an unmapped key is distinguishable from a real classification: it
  reports `mapped=False`, zero confidence, a LOW band, and may not exclude an
  instrument. The classes shown are illustrative while the taxonomy is a stub.
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

import typer

from recovery.classifier import DEFAULT_CLASSIFIER_PATH, load_classifier
from recovery.config import DEFAULT_CONFIG_PATH, load_config
from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway
from recovery.ingest import ingest_delivery
from recovery.normalize import normalize_entity
from allocator.arm_c import ArmC
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import calendar_from_config
from recovery.sim.horizon import SurvivalBasis, horizon_sweep
from recovery.sim.run import mandate_survival_dominance, run_comparison
from recovery.sim.world import load_world_config, mandate_hazard_range, sample_world
from recovery.store import Store
from recovery.worker import process_pending

app = typer.Typer(add_completion=False, help=__doc__)

REPLAY_COUNT = 10


def _fresh_store(path: pathlib.Path) -> Store:
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            candidate.unlink()
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
) -> None:
    config = load_config(config_path)
    # allow_stub: the taxonomy is not authored yet. The gate exists so this is a
    # deliberate choice rather than an accident -- and it is stated in the output.
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

    stats = process_pending(store, config, gateway, classifier)

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

    passed = _c2_section(classifier) and passed
    passed = _c5_section(config, classifier) and passed

    store.close()
    typer.echo("\nreproduce: " + ("OK" if passed else "FAILED"))
    if not passed:
        sys.exit(1)


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


SIM_SEED = 42


def _c5_section(config, classifier) -> bool:
    """C5: all three arms over one sampled world."""
    world = sample_world(seed=SIM_SEED)
    calendar = calendar_from_config(config.regulatory)
    arms = [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)]
    result = run_comparison(arms, world, calendar, costs=classifier.config.costs)

    typer.echo(f"\nC5 simulator -- arms A and B, world seed {SIM_SEED}\n")
    typer.echo(
        "  NOTE: one world. Every cardinal value below is a single draw from the "
        "ranges in\n        config/worlds.yaml. A defensible figure needs C8's "
        "sweep across many worlds;\n        these numbers are the harness working, "
        "not a result."
    )
    typer.echo(
        f"\n    {'arm':<5}{'recovered_INR':>15}{'attempts':>10}{'contacts':>10}"
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

    # Three arms so the uplift decomposes. A -> C alone would conflate the value
    # of contacting people with the value of knowing why they failed.
    typer.echo(
        f"\n    A -> B  contact uplift:          Rs {result.uplift('B', 'A'):>12,.2f}"
        "   (value of contacting people)"
    )
    typer.echo(
        f"    B -> C  cause-awareness uplift: Rs {result.uplift('C', 'B'):>12,.2f}"
        "   (value of knowing why)"
    )
    typer.echo(
        f"\n    attempts spent where recovery is impossible: "
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

    # Classifier coverage bounds what the allocator can do. An unmapped key
    # lands in the LOW row, which is correct and conservative -- one link, no
    # execution. The more of the batch is unmapped, the more Arm C behaves like
    # a link-only arm regardless of how good the decision table is. Printed
    # because a money figure for Arm C is uninterpretable without it.
    arm_c = ArmC(calendar, classifier, config)
    coverage = _coverage(arm_c, world)
    typer.echo(
        f"\n    classifier coverage on this batch: {coverage:.0%} mapped "
        f"({1 - coverage:.0%} fall to the LOW row)"
    )
    if classifier.config.is_stub:
        typer.echo(
            "    ^ the taxonomy is a STUB. Arm C's money figure is bounded by this,\n"
            "      not by the decision table. Do not read it as the allocator's ceiling."
        )

    # Mandate survival: an ordering, not a count. The count depends on an
    # invented revocation hazard; the ordering survives the whole range.
    dominance = mandate_survival_dominance(
        [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)],
        world,
        calendar,
        mandate_hazard_range(load_world_config()),
        costs=classifier.config.costs,
    )
    typer.echo("\n  mandate survival -- reported as dominance, never as a count:")
    typer.echo(f"    {dominance.describe()}")
    typer.echo(
        f"    ordering inversions across the range: {dominance.inversions}"
        + (
            f", crossing at {dominance.crossover_points()}"
            if dominance.crossover_points()
            else ""
        )
    )
    typer.echo(
        "    (no mandate count is printed anywhere: it would rest on a revocation\n"
        "     rate nobody publishes. The ordering does not need one.)\n"
    )

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
