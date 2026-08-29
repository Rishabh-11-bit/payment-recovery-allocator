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
from recovery.models import ConfidenceBand
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


@dataclass
class BandRow:
    """One confidence band's outcomes, pooled across seeds."""

    band: ConfidenceBand
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class BandAccuracyReport:
    """Does confidence predict correctness? -- and the one comparison that
    would be dishonest to make, named rather than silently included.

    HIGH and MODERATE are real classification attempts: `failure_class` is
    the rule's answer, and comparing it to ground truth asks the question the
    band claims to answer -- is a HIGH-confidence guess right more often than
    a MODERATE one, which is what "confidence" is supposed to mean.

    LOW is different in kind, not just in threshold. `failure_class` on a LOW
    row is the cost model's minimax-safe fallback -- deliberately not the
    rule's guess, precisely because the guess is not trusted. Scoring that
    against ground truth would grade a guess the system explicitly declined
    to make. What CAN be asked of a LOW row that had a rule match is whether
    the rule's raw guess (`cost_resolved_from`) would have been right --
    informational only, since the system never acts on it.
    """

    high: BandRow
    moderate: BandRow
    # LOW rows that matched a rule (mapped=True): how often the rule's own
    # untrusted guess would have been correct, had it been trusted. Not
    # compared against HIGH/MODERATE -- it answers a different question.
    low_rule_guess: BandRow
    seeds: tuple[int, ...]

    @property
    def monotonic(self) -> bool:
        """The ordering a confidence band should produce, if it means anything."""
        if not (self.high.total and self.moderate.total):
            return True  # nothing to violate
        return self.high.accuracy >= self.moderate.accuracy


def band_accuracy(
    classifier: Classifier,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    *,
    worlds_path: pathlib.Path | str = DEFAULT_WORLDS_PATH,
) -> BandAccuracyReport:
    """Pool the same batches `analyse` draws and score confidence against truth.

    Circularity caveat is the same one `analyse`'s docstring already states:
    ground truth here is the simulator's own emission table, hand-written by
    the same person who wrote the taxonomy. Agreement is corroborating, not
    proof -- and the confidence bands are themselves authored from reading
    Razorpay's documentation, not fit to this or any dataset, which is what
    makes the question worth asking rather than circular by construction: an
    authored ordering either predicts correctness or it does not, and nothing
    here adjusts the bands to make it so.
    """
    raw = load_world_config(worlds_path)
    high = BandRow(band=ConfidenceBand.HIGH)
    moderate = BandRow(band=ConfidenceBand.MODERATE)
    low_guess = BandRow(band=ConfidenceBand.LOW)

    for seed in seeds:
        world = sample_world(seed=seed, raw=raw)
        for failure in generate_batch(world):
            key = normalize_entity(
                failure.observed(), source_space=classifier.config.source_space
            )
            result = classifier.classify(key)
            truth = failure.true_class.value

            if result.band is ConfidenceBand.HIGH:
                high.total += 1
                high.correct += result.failure_class.value == truth
            elif result.band is ConfidenceBand.MODERATE:
                moderate.total += 1
                moderate.correct += result.failure_class.value == truth
            elif result.mapped and result.cost_resolved_from is not None:
                low_guess.total += 1
                low_guess.correct += result.cost_resolved_from.value == truth

    return BandAccuracyReport(
        high=high, moderate=moderate, low_rule_guess=low_guess, seeds=seeds
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
    seed_tuple = tuple(int(s) for s in seeds.split(","))
    report = analyse(classifier, seed_tuple)

    typer.echo(
        f"Classifier coverage over {report.total:,} synthetic failures "
        f"({len(report.seeds)} world seeds)\n"
    )
    typer.echo(f"  mapped by a rule : {report.mapped:>6,}  ({report.coverage:.1%})")

    _echo_band_accuracy(classifier, seed_tuple)
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


def _echo_band_accuracy(classifier: Classifier, seeds: tuple[int, ...]) -> None:
    """Does confidence predict correctness? Printed once, right after coverage,
    because a coverage percentage with no calibration check reads as more
    trustworthy than it has earned."""
    bacc = band_accuracy(classifier, seeds)
    typer.echo("")
    typer.echo("  confidence vs. correctness (does the band mean what it claims?)")
    typer.echo("")
    for row, label in ((bacc.high, "HIGH"), (bacc.moderate, "MODERATE")):
        typer.echo(
            f"    {label:<10} accuracy: {row.correct:>5,} / {row.total:<5,}  "
            f"({row.accuracy:.1%})"
        )
    typer.echo(
        f"    {'monotonic':<10} {'HIGH >= MODERATE' if bacc.monotonic else '*** VIOLATED ***'}"
    )
    if bacc.low_rule_guess.total:
        typer.echo(
            f"\n    LOW band, informational only -- the rule's own guess before cost\n"
            f"    resolution, which the system never acts on: {bacc.low_rule_guess.correct:,} "
            f"/ {bacc.low_rule_guess.total:,} ({bacc.low_rule_guess.accuracy:.1%})"
        )
        if bacc.low_rule_guess.accuracy >= bacc.high.accuracy and bacc.high.total:
            typer.echo(
                "\n    That LOW figure is not evidence the deliberately-low-confidence rows\n"
                "    are actually confident. 56% of this sample is one key --\n"
                "    upi/beneficiary_bank/payment_debit_response/mandate_revoked -- whose\n"
                "    note names a real ambiguity: the code can mean a genuine customer\n"
                "    revocation, or the bank surfacing its own mandate-state error under\n"
                "    the same reason. The simulator's emission table has no channel for the\n"
                "    second cause at all -- this key is emitted by exactly one true class.\n"
                "    So the simulator cannot produce the case the row exists to guard\n"
                "    against, and a high score here confirms only that the simulator is\n"
                "    structurally unable to test the claim, not that the claim was wrong.\n"
                "    See CHALLENGES.md 019."
            )
    if not bacc.moderate.total:
        typer.echo(
            "\n    MODERATE is 0/0, and that is two different facts wearing one number.\n"
            "    Two MODERATE rules (mandate_creation_expired, mandate_creation_timeout)\n"
            "    are genuinely unreachable -- no ingest path produces them at all, see the\n"
            "    UNREACHABLE ROWS section below. The other two (card_enrollment_check,\n"
            "    reqauth_mandate_not_acknowledged) are real and reachable in production;\n"
            "    `EMISSIONS` in sim/batch.py is a fixed subset of the documented reason\n"
            "    codes and simply never happens to emit either. The zero here says nothing\n"
            "    about whether those two rules are good rules -- only that this synthetic\n"
            "    generator has never exercised them."
        )
    typer.echo(
        "\n    Ground truth is the simulator's own emission table -- corroborating,\n"
        "    not proof, and the caveat above the unmapped-keys list applies here too.\n"
        "    The bands are authored from documentation, never fit to this data, which\n"
        "    is what makes the ordering worth checking rather than circular."
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
