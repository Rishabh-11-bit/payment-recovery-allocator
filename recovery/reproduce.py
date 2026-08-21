"""`python -m recovery.reproduce` -- regenerates every claim this repo makes.

Today that is one claim, C1's: **replaying one event ten times produces exactly
one case and one decision, with the full sequence visible in the audit trail.**

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

from recovery.config import DEFAULT_CONFIG_PATH, load_config
from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway
from recovery.ingest import ingest_delivery
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
    db_path: pathlib.Path = typer.Option(
        pathlib.Path("data/reproduce.db"), "--db", help="Recreated from scratch on every run."
    ),
) -> None:
    config = load_config(config_path)
    store = _fresh_store(db_path)
    gateway = SimulatedGateway()

    typer.echo("C1 event core -- replay of a single event\n")

    headers, body = build_delivery(event_id="evt_REPRODUCE00001")
    gateway.seed_from_webhook(body["payload"]["payment"]["entity"])

    outcomes: list[str] = []
    for _ in range(REPLAY_COUNT):
        outcomes.append(ingest_delivery(store, config, headers, body).outcome.value)

    stats = process_pending(store, config, gateway)

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

    store.close()
    typer.echo("\nreproduce: " + ("OK" if passed else "FAILED"))
    if not passed:
        sys.exit(1)


def _summarise(outcomes: list[str]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return ", ".join(f"{count}x {name}" for name, count in sorted(counts.items()))


def _detail(detail: dict) -> str:
    keys = ("action", "authoritative_status", "dedup_key", "attempt_n", "chain_key")
    shown = {k: detail[k] for k in keys if k in detail}
    return " ".join(f"{k}={v}" for k, v in shown.items())


if __name__ == "__main__":
    app()
