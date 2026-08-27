"""Which classifier keys the taxonomy does not cover.

    python -m recovery.coverage

Ranks every `(method, source, step, reason)` the simulator emits that matches no
rule, most frequent first, with the ground-truth class distribution alongside.

---

**Read the two columns differently.** They have very different standing.

*Frequency* is a fact about the key space: these tuples occur, in roughly this
proportion, and every one of them currently falls to the LOW row. Writing rows
against this list is coverage work and is exactly the right use of it.

*True class* is the simulator's ground truth, and the simulator's emission
table was written by hand. Writing rows to match that column would fit the
taxonomy to the generator, and the resulting accuracy would measure nothing
except that both were typed by the same person -- CHALLENGES 002, arriving
through a side door. Treat it as a cross-check on reasoning you arrived at from
the documentation and the captures, and be suspicious when it agrees too well.

The captured section at the end is not synthetic and carries no such caveat.
"""

from __future__ import annotations

import collections
import pathlib
from dataclasses import dataclass, field

import typer

from recovery.classifier import DEFAULT_CLASSIFIER_PATH, Classifier, load_classifier
from recovery.fixtures import load_captured_payments
from recovery.normalize import normalize_entity
from recovery.sim.batch import generate_batch
from recovery.sim.world import DEFAULT_WORLDS_PATH, load_world_config, sample_world

app = typer.Typer(add_completion=False, help=__doc__)

DEFAULT_SEEDS = (11, 42, 101, 202, 303)


@dataclass
class KeyStats:
    key: tuple[str | None, ...]
    rail: str
    count: int = 0
    true_classes: collections.Counter = field(default_factory=collections.Counter)

    @property
    def describe(self) -> str:
        return "/".join(part or "-" for part in self.key)

    def true_class_summary(self) -> str:
        total = sum(self.true_classes.values()) or 1
        parts = [
            f"{name} {value * 100 // total}%"
            for name, value in self.true_classes.most_common()
        ]
        return ", ".join(parts)

    @property
    def dominant_true_class(self) -> str:
        return self.true_classes.most_common(1)[0][0] if self.true_classes else "-"

    @property
    def purity(self) -> float:
        total = sum(self.true_classes.values()) or 1
        return self.true_classes.most_common(1)[0][1] / total if self.true_classes else 0.0


@dataclass
class CoverageReport:
    total: int
    mapped: int
    unmapped_keys: list[KeyStats]
    mapped_low_band: int
    seeds: tuple[int, ...]

    @property
    def unmapped(self) -> int:
        return self.total - self.mapped

    @property
    def coverage(self) -> float:
        return self.mapped / self.total if self.total else 0.0


def analyse(
    classifier: Classifier,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    *,
    worlds_path: pathlib.Path | str = DEFAULT_WORLDS_PATH,
) -> CoverageReport:
    """Classify several batches and collect what missed.

    Several seeds rather than one: a single batch's key mix is a draw, and a
    taxonomy written against one draw inherits its accidents.
    """
    raw = load_world_config(worlds_path)
    unmapped: dict[tuple, KeyStats] = {}
    total = mapped = mapped_low = 0

    for seed in seeds:
        world = sample_world(seed=seed, raw=raw)
        for failure in generate_batch(world):
            total += 1
            key = normalize_entity(
                failure.observed(), source_space=classifier.config.source_space
            )
            result = classifier.classify(key)
            if result.mapped:
                mapped += 1
                if result.band.value == "LOW":
                    mapped_low += 1
                continue
            stats = unmapped.setdefault(
                key.key, KeyStats(key=key.key, rail=failure.rail)
            )
            stats.count += 1
            stats.true_classes[failure.true_class.value] += 1

    return CoverageReport(
        total=total,
        mapped=mapped,
        unmapped_keys=sorted(unmapped.values(), key=lambda s: -s.count),
        mapped_low_band=mapped_low,
        seeds=seeds,
    )


@app.command()
def main(
    classifier_path: pathlib.Path = typer.Option(DEFAULT_CLASSIFIER_PATH, "--classifier"),
    seeds: str = typer.Option(
        ",".join(str(s) for s in DEFAULT_SEEDS),
        "--seeds",
        help="Comma-separated world seeds to pool.",
    ),
) -> None:
    classifier = load_classifier(classifier_path, allow_stub=True)
    report = analyse(classifier, tuple(int(s) for s in seeds.split(",")))

    typer.echo(
        f"Classifier coverage over {report.total:,} synthetic failures "
        f"({len(report.seeds)} world seeds)\n"
    )
    typer.echo(f"  mapped by a rule : {report.mapped:>6,}  ({report.coverage:.1%})")
    typer.echo(
        f"  unmapped         : {report.unmapped:>6,}  ({1 - report.coverage:.1%})"
        "   -> all of these fall to the LOW row"
    )
    typer.echo(
        f"  mapped but LOW   : {report.mapped_low_band:>6,}"
        "   (a rule matched, at low confidence -- these already have rows)"
    )

    if not report.unmapped_keys:
        typer.echo("\n  Nothing unmapped. Every emitted key matches a rule.")
        _echo_captured(classifier)
        _echo_unreachable(classifier)
        return

    typer.echo(
        f"\n\n  UNMAPPED KEYS, most frequent first ({len(report.unmapped_keys)} distinct)\n"
    )
    typer.echo(
        f"    {'#':>5}  {'share':>6}  {'rail':<9} "
        f"{'method/source/step/reason':<62} ground truth"
    )
    typer.echo("    " + "-" * 118)
    for stats in report.unmapped_keys:
        share = stats.count / report.total
        typer.echo(
            f"    {stats.count:>5}  {share:>6.1%}  {stats.rail:<9} "
            f"{stats.describe:<62} {stats.true_class_summary()}"
        )

    _echo_ambiguity(report)
    _echo_captured(classifier)
    _echo_unreachable(classifier)

    typer.echo(
        "\n  The frequency column is a fact about the key space and is what these\n"
        "  rows should be written against. The ground-truth column comes from a\n"
        "  hand-written emission table -- matching rows to it fits the taxonomy to\n"
        "  the generator, which measures nothing. Use it to check reasoning you\n"
        "  reached another way, and be suspicious where it agrees too well.\n"
    )


def _echo_unreachable(classifier: Classifier) -> None:
    """Rows that exist for keys no ingested event can produce.

    Printed here because the coverage figure would otherwise flatter itself.
    These rows raise the rule count and cover nothing that arrives, so a reader
    comparing a rule count against a coverage percentage should be able to see
    which rules were never in a position to contribute to it.
    """
    unreachable = classifier.config.unreachable_rules
    if not unreachable:
        return
    total = len(classifier.config.rules)
    typer.echo("")
    typer.echo(
        f"\n  UNREACHABLE ROWS ({len(unreachable)} of {total})"
        " -- authored, correct, and never exercised"
    )
    typer.echo("")
    for rule in unreachable:
        match = ", ".join(f"{k}={v}" for k, v in sorted(rule.match.items()))
        typer.echo(f"    {match:<52} -> {rule.failure_class.value}")
    typer.echo("")
    typer.echo(
        "    `payment.failed` is not triggered on an authorisation failure, and"
    )
    typer.echo(
        "    mandate registration fails at authorisation. Reaching these needs"
    )
    typer.echo(
        "    `subscription.pending` ingestion -- see NOT_BUILT.md. They are counted"
    )
    typer.echo(
        "    in the rule total and contribute nothing to the coverage figure above."
    )


def _echo_ambiguity(report: CoverageReport) -> None:
    """Keys whose ground truth is genuinely mixed cannot be classified cleanly."""
    ambiguous = [s for s in report.unmapped_keys if s.purity < 0.9 and s.count > 5]
    if not ambiguous:
        return
    typer.echo(
        "\n\n  KEYS WITH MIXED GROUND TRUTH -- no single row can be right for these\n"
    )
    for stats in ambiguous:
        typer.echo(
            f"    {stats.describe:<62} dominant {stats.dominant_true_class} "
            f"at only {stats.purity:.0%}"
        )
    typer.echo(
        "\n    A row here buys a confident-looking answer that is wrong a large\n"
        "    share of the time. A deliberately low confidence -- landing these in\n"
        "    the LOW row on purpose rather than by omission -- may be the better\n"
        "    row, and it is a different statement from having no row at all."
    )


def _echo_captured(classifier: Classifier) -> None:
    """Real keys. No emission table behind these, so no circularity."""
    captured = load_captured_payments()
    if not captured:
        return
    typer.echo("\n\n  CAPTURED REAL KEYS (not synthetic -- no ground truth available)\n")
    seen: dict[str, bool] = {}
    for entity in captured:
        key = normalize_entity(entity, source_space=classifier.config.source_space)
        result = classifier.classify(key)
        if key.describe() in seen:
            continue
        seen[key.describe()] = result.mapped
        flag = "mapped" if result.mapped else "UNMAPPED"
        undocumented = " [source undocumented]" if result.source_undocumented else ""
        typer.echo(f"    {flag:<9} {key.describe()}{undocumented}")


if __name__ == "__main__":
    app()
