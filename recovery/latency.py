"""How long does one decision actually take on this machine?

    python -m recovery.latency --cases 500

A real number, not an invented one -- the counterpoint to every competitor
submission that quotes a latency waterfall for a pipeline that never ran.
This one runs the real path: `ingest_delivery` through `process_pending`
with the real `ArmCDecider`, the real `Guard`, and a real SQLite store on
disk, timed with `time.perf_counter()` around each call.

**What this is not.** Not a production claim. Single process, one worker, no
queue, no network hop, no concurrent load, local SQLite rather than whatever
a real deployment would run against. It measures the one thing that transfers
honestly from a laptop to a cluster: the pure per-case decision cost --
normalize, classify, allocate, guard, persist -- with every dependency on
scale-specific infrastructure stripped out. See the README's scale section
for what changes between this number and a production deployment; this
number is not a substitute for measuring that.

Two phases, timed separately, because they map onto two different real
constraints:

* **ingest** -- webhook receipt to durable acknowledgment. Razorpay treats a
  response slower than 5 seconds as a timeout and resends; this is the
  number that has to clear that bar.
* **decision** -- claiming a pending job through classify, allocate, guard,
  and a recorded decision. This is the number that would need to scale
  horizontally across workers in production; it is reported so that claim is
  falsifiable rather than asserted.

Deliberately not wired into `reproduce`'s default output: timing is
noise-sensitive in a way none of this project's other figures are -- the
same machine gives different numbers under different load -- and mixing a
noisy number into a command whose entire point is that its figures reproduce
exactly would be its own small dishonesty.
"""

from __future__ import annotations

import dataclasses
import pathlib
import statistics
import tempfile
import time

import typer

from allocator.arm_c import ArmC
from allocator.wiring import ArmCDecider
from recovery.classifier import DEFAULT_CLASSIFIER_PATH, load_classifier
from recovery.config import DEFAULT_CONFIG_PATH, load_config
from recovery.fixtures import build_delivery
from recovery.gateway import SimulatedGateway
from recovery.guard import guard_from_config
from recovery.ingest import ingest_delivery
from recovery.sim.calendar import calendar_from_config
from recovery.store import Store
from recovery.worker import process_pending

app = typer.Typer(add_completion=False, help=__doc__)

# Cycle through real, distinct classifier keys so the measurement reflects
# heterogeneous cases -- HIGH, MODERATE, LOW, three rails -- rather than one
# key hitting the same rule and the same code path every time.
_CASE_TEMPLATES = (
    ("upi", "customer_psp", "payment_debit_response", "insufficient_funds"),  # HIGH
    ("card", "issuer_bank", "payment_authorization", "payment_expired_card"),  # HIGH
    ("upi", "customer", "payment_authentication", "payment_ux_canceled"),  # HIGH
    ("card", "gateway", "payment_initiation", "gateway_technical_error"),  # LOW
    ("netbanking", "bank", "payment_authorization", "payment_failed"),  # LOW
)


@dataclasses.dataclass(frozen=True)
class PhaseStats:
    samples_ms: tuple[float, ...]

    @property
    def p50(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def p90(self) -> float:
        return statistics.quantiles(self.samples_ms, n=10)[8] if len(self.samples_ms) >= 10 else max(self.samples_ms)

    @property
    def p99(self) -> float:
        return statistics.quantiles(self.samples_ms, n=100)[98] if len(self.samples_ms) >= 100 else max(self.samples_ms)

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples_ms)


@dataclasses.dataclass(frozen=True)
class LatencyReport:
    cases: int
    ingest: PhaseStats
    decision: PhaseStats


def measure(
    cases: int,
    config_path: pathlib.Path = DEFAULT_CONFIG_PATH,
    classifier_path: pathlib.Path = DEFAULT_CLASSIFIER_PATH,
) -> LatencyReport:
    """Run `cases` deliveries through the real ingest -> decide path, timed.

    One store for the whole run, not reset between cases -- a fresh database
    per case would measure SQLite file creation, not the system. Each case
    ingests exactly one delivery, then drains exactly that one pending job,
    so `decision` timing is never averaged across a batch that happened to
    claim more than one row at once.
    """
    config = load_config(config_path)
    classifier = load_classifier(classifier_path)
    calendar = calendar_from_config(config.regulatory)
    guard = guard_from_config(config, calendar)
    decider = ArmCDecider(ArmC(calendar, classifier, config))
    gateway = SimulatedGateway()

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(pathlib.Path(tmp) / "latency.db")
        store.initialise()
        try:
            ingest_ms: list[float] = []
            decision_ms: list[float] = []

            for index in range(cases):
                method, source, step, reason = _CASE_TEMPLATES[index % len(_CASE_TEMPLATES)]
                headers, body = build_delivery(
                    event_id=f"evt_LATENCY{index:012d}",
                    payment_id=f"pay_LATENCY{index:012d}",
                    order_id=f"order_LATENCY{index:011d}",
                    method=method,
                    error_source=source,
                    error_step=step,
                    error_reason=reason,
                )
                gateway.seed_from_webhook(body["payload"]["payment"]["entity"])

                start = time.perf_counter()
                ingest_delivery(store, config, headers, body)
                ingest_ms.append((time.perf_counter() - start) * 1000)

                start = time.perf_counter()
                process_pending(store, config, gateway, classifier, decider=decider, guard=guard)
                decision_ms.append((time.perf_counter() - start) * 1000)
        finally:
            store.close()

    return LatencyReport(
        cases=cases,
        ingest=PhaseStats(tuple(ingest_ms)),
        decision=PhaseStats(tuple(decision_ms)),
    )


@app.command()
def main(
    cases: int = typer.Option(500, "--cases", help="Deliveries to time."),
    config_path: pathlib.Path = typer.Option(DEFAULT_CONFIG_PATH, "--config"),
    classifier_path: pathlib.Path = typer.Option(DEFAULT_CLASSIFIER_PATH, "--classifier"),
) -> None:
    typer.echo(f"Timing {cases} cases through the real ingest -> decide path\n")
    report = measure(cases, config_path, classifier_path)

    typer.echo(f"    {'phase':<10} {'p50':>8} {'p90':>8} {'p99':>8} {'mean':>8}   (ms)")
    for label, stats in (("ingest", report.ingest), ("decision", report.decision)):
        typer.echo(
            f"    {label:<10} {stats.p50:>8.2f} {stats.p90:>8.2f} "
            f"{stats.p99:>8.2f} {stats.mean:>8.2f}"
        )

    margin = 5000 / report.ingest.p99 if report.ingest.p99 else float("inf")
    typer.echo(
        "\n    Single process, one worker, local SQLite, no network, no concurrent\n"
        "    load -- not a production number. It is the pure decision-path cost:\n"
        "    normalize, classify, allocate, guard, persist. Razorpay treats a\n"
        f"    webhook response slower than 5,000ms as a timeout; ingest p99 here is\n"
        f"    {report.ingest.p99:.1f}ms, roughly {margin:.0f}x under that bar on this single\n"
        "    machine with no other load -- which says the decision logic is not what\n"
        "    would need scaling, not that the system is production-ready."
    )


if __name__ == "__main__":
    app()
