"""`python -m recovery.explain <case_id>` -- why did this case get this decision?

The audit trail exists so that question has an answer. This is the answer's
interface.

    python -m recovery.explain case_abc123          # a case
    python -m recovery.explain order_TSjMFtKQ8RFoQY # or the order
    python -m recovery.explain pay_TSjVZi1gipZs5L   # or any payment on its chain
    python -m recovery.explain --list               # what is in the ledger
    python -m recovery.explain --summary            # counts and guard blocks

Identifier resolution is deliberate: whoever asks "why did this happen" has
whichever id the complaint arrived with -- usually a payment id from a customer,
sometimes an order id from a merchant, almost never our internal case id.

The default view hides routine events and says how many it hid. `--verbose`
shows the raw trail. Both come from the same append-only tables; the compact
view is a filter over the record, never a different record.
"""

from __future__ import annotations

import pathlib
import sys

import typer

from recovery.config import DEFAULT_CONFIG_PATH, load_config
from recovery.ledger import Ledger, render_trace
from recovery.store import Store

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    identifier: str = typer.Argument(
        "", help="Case id, order id, or payment id. Omit with --list or --summary."
    ),
    db: pathlib.Path = typer.Option(
        None, "--db", help="Ledger database. Defaults to the configured path."
    ),
    config_path: pathlib.Path = typer.Option(DEFAULT_CONFIG_PATH, "--config"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every event."),
    list_cases: bool = typer.Option(False, "--list", help="List case ids."),
    summary: bool = typer.Option(False, "--summary", help="Ledger counts."),
    limit: int = typer.Option(20, "--limit", help="Cases to list."),
) -> None:
    database = db or pathlib.Path(load_config(config_path).database.path)
    if not database.exists():
        typer.echo(
            f"No ledger at {database}. Run `python -m recovery.reproduce` first, or "
            "pass --db.",
            err=True,
        )
        raise typer.Exit(code=2)

    store = Store(database)
    ledger = Ledger(store)
    try:
        if summary:
            _echo_summary(ledger)
            return
        if list_cases or not identifier:
            _echo_list(ledger, limit)
            return

        case_id = ledger.resolve(identifier)
        if case_id is None:
            typer.echo(f"No case found for {identifier!r}.", err=True)
            typer.echo("Try --list to see what the ledger holds.", err=True)
            raise typer.Exit(code=1)

        trace = ledger.trace(case_id)
        if trace is None:
            typer.echo(f"Case {case_id} resolved but has no record.", err=True)
            raise typer.Exit(code=1)

        if case_id != identifier:
            typer.echo(f"{identifier} resolves to {case_id}\n")
        typer.echo(render_trace(trace, verbose=verbose))
    finally:
        store.close()


def _echo_summary(ledger: Ledger) -> None:
    counts = ledger.summary()
    typer.echo("ledger summary\n")
    for name, value in counts.items():
        typer.echo(f"  {name:<16}{value:>8,}")

    typer.echo("\n  audit events by type:")
    for event_type, count in ledger.event_counts():
        typer.echo(f"    {event_type:<38}{count:>7,}")

    blocks = ledger.block_reasons()
    if blocks:
        # What the system wanted to do and was not allowed to. A guard that
        # blocks nothing is either perfect or not running, and this is how the
        # difference shows.
        typer.echo("\n  guard blocks by reason:")
        for reason, count in blocks:
            typer.echo(f"    {reason:<38}{count:>7,}")


def _echo_list(ledger: Ledger, limit: int) -> None:
    case_ids = ledger.case_ids(limit=limit)
    if not case_ids:
        typer.echo("Ledger holds no cases.")
        return
    typer.echo(f"{len(case_ids)} case(s):\n")
    for case_id in case_ids:
        trace = ledger.trace(case_id)
        if trace is None:
            continue
        typer.echo(f"  {case_id}  {trace.state:<26} {trace.outcome_line()}")


if __name__ == "__main__":
    app()
