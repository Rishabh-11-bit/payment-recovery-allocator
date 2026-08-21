"""`python -m recovery.reproduce` -- regenerates every claim this repo makes.

Today that is two:

* **C1** -- replaying one event ten times produces exactly one case and one
  decision, with the full sequence visible in the audit trail.
* **C2** -- an unmapped key is distinguishable from a real classification: it
  reports `mapped=False`, zero confidence, a LOW band, and may not exclude an
  instrument. The classes shown are illustrative while the taxonomy is a stub.

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
