"""The contract C3 has to satisfy.

Scaffolding, not policy. Nothing here decides anything — it provides the
fixtures an allocator is tested against, a harness that checks its proposals
stay inside the mandate-execution budget, and a static check that it reasons
about orderings rather than magnitudes.

Three pieces:

* `classification_grid()` — all twelve `FailureClass` x `ConfidenceBand`
  combinations, plus the edge cases the captures turned up. An allocator has to
  do something defensible with every one of them.
* `execution_budget_violations()` — drives an arm over a batch and reports any
  chain where system-initiated executions exceeded the cap.
* `ordinal_violations()` — parses the allocator's source and reports anything
  that looks like it is reading a probability instead of an ordering.

The budget harness counts **ATTEMPT proposals only**. Contacts are not mandate
executions and do not consume the NPCI budget — see CHALLENGES 008 and the
"Two counters, not one" note in CLAUDE.md. The environment does not yet make
that distinction; this harness does, so an allocator written against it is
written against the right counter.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from recovery.classifier import Classifier
from recovery.models import (
    Classification,
    ConfidenceBand,
    FailureClass,
    NormalizedFailure,
)
from recovery.sim.arms import Arm
from recovery.sim.batch import SyntheticFailure, generate_batch
from recovery.sim.calendar import IST, ComplianceCalendar
from recovery.sim.environment import ActionKind, CaseOutcome, CaseView, Proposal
from recovery.sim.world import World

# 1 initial execution has already been spent -- that failure is what opened the
# case -- so this is what an allocator has left to spend.
def remaining_execution_budget(calendar: ComplianceCalendar) -> int:
    return calendar.attempt_cap - 1


# --------------------------------------------------------------------------- #
# Fixtures: the twelve combinations
# --------------------------------------------------------------------------- #


def confidence_for_band(classifier: Classifier, band: ConfidenceBand) -> float:
    """A confidence that lands squarely inside `band` for this configuration.

    Read from the loaded thresholds rather than hardcoded, so the grid follows
    the config instead of drifting from it.
    """
    high = classifier.config.high_threshold
    moderate = classifier.config.moderate_threshold
    if band is ConfidenceBand.HIGH:
        return min(1.0, (high + 1.0) / 2)
    if band is ConfidenceBand.MODERATE:
        return (moderate + high) / 2
    return moderate / 2


def make_classification(
    classifier: Classifier,
    failure_class: FailureClass,
    band: ConfidenceBand,
    *,
    mapped: bool = True,
    cause_family: str | None = None,
    source_undocumented: bool = False,
    key: NormalizedFailure | None = None,
) -> Classification:
    """Build a classification directly, bypassing the taxonomy.

    Deliberate: the allocator must handle every (class, band) pair it could be
    handed, and the stub taxonomy does not produce all twelve. Testing the
    allocator through the real table would test the table.
    """
    return Classification(
        failure_class=failure_class,
        confidence=confidence_for_band(classifier, band),
        band=band,
        key=key
        or NormalizedFailure(
            method="upi",
            source="customer_psp",
            step="payment_debit_response",
            reason="synthetic_contract_fixture",
        ),
        mapped=mapped,
        rule_index=0 if mapped else None,
        cause_family=cause_family,
        source_undocumented=source_undocumented,
    )


def classification_grid(classifier: Classifier) -> dict[tuple[str, str], Classification]:
    """All twelve class x band combinations, keyed `(class, band)`.

    Note what a LOW band means here. From the real classifier a LOW band has
    already been cost-resolved, so `failure_class` is the safest class rather
    than the predicted one. The grid does **not** assume that: it pairs every
    class with every band, including combinations the current table cannot
    produce. An allocator that only works on the pairs today's stub emits will
    break the first time the taxonomy is edited.
    """
    return {
        (failure_class.value, band.value): make_classification(
            classifier, failure_class, band
        )
        for failure_class in FailureClass
        for band in ConfidenceBand
    }


def edge_classifications(classifier: Classifier) -> dict[str, Classification]:
    """The cases real captures turned up. Each needs a defensible answer.

    These are not hypotheticals -- every one corresponds to a payload in
    `tests/fixtures/payments/`.
    """
    return {
        # No rule matched. Cost-resolved, zero confidence, LOW band.
        "unmapped": make_classification(
            classifier, FailureClass.INFRASTRUCTURE, ConfidenceBand.LOW, mapped=False
        ),
        # Expired card: TERMINAL, but a card-change offer can still recover it.
        "terminal_instrument": make_classification(
            classifier,
            FailureClass.TERMINAL,
            ConfidenceBand.HIGH,
            cause_family="instrument",
        ),
        # international_transaction_not_allowed: TERMINAL, and nothing the
        # customer does will help. A recovery link here is pure waste.
        "terminal_merchant_configuration": make_classification(
            classifier,
            FailureClass.TERMINAL,
            ConfidenceBand.HIGH,
            cause_family="merchant_configuration",
        ),
        # The generic netbanking decline. No information in the payload.
        "generic_decline": make_classification(
            classifier,
            FailureClass.LIQUIDITY,
            ConfidenceBand.LOW,
            cause_family="generic_decline",
        ),
        # Source outside its method's documented space: surfaced, not rejected.
        "source_undocumented": make_classification(
            classifier,
            FailureClass.LIQUIDITY,
            ConfidenceBand.MODERATE,
            source_undocumented=True,
        ),
    }


class FixedClassifier:
    """Returns one classification regardless of input.

    Lets an arm be driven through `propose()` against a chosen (class, band)
    without depending on the taxonomy. Shape-compatible with `Classifier` for
    the two attributes an arm needs.
    """

    def __init__(self, classification: Classification, config) -> None:
        self._classification = classification
        self.config = config

    def classify(self, key: NormalizedFailure) -> Classification:
        del key
        return self._classification


def make_case_view(
    *,
    case_id: str = "case_contract",
    rail: str = "upi",
    amount_paise: int = 49900,
    failed_at: dt.datetime | None = None,
    observed: dict | None = None,
    attempts_used: int = 1,
    contacts_used: int = 0,
    outcome: CaseOutcome = CaseOutcome.OPEN,
    attempt_pending: bool = False,
    last_attempt_resolved_at: dt.datetime | None = None,
) -> CaseView:
    """A case view with sensible defaults. `attempts_used=1` is a fresh case."""
    failed_at = failed_at or dt.datetime(2026, 3, 2, 3, 0, tzinfo=IST)
    return CaseView(
        case_id=case_id,
        rail=rail,
        amount_paise=amount_paise,
        failed_at=failed_at,
        observed=observed
        or {
            "id": "pay_contract",
            "method": rail,
            "status": "failed",
            "error_source": "customer_psp",
            "error_step": "payment_debit_response",
            "error_reason": "insufficient_funds",
        },
        attempts_used=attempts_used,
        contacts_used=contacts_used,
        outcome=outcome,
        attempt_pending=attempt_pending,
        last_attempt_resolved_at=last_attempt_resolved_at,
    )


# --------------------------------------------------------------------------- #
# Budget harness
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BudgetViolation:
    case_id: str
    executions_proposed: int
    budget: int

    def __str__(self) -> str:
        return (
            f"{self.case_id}: proposed {self.executions_proposed} mandate executions "
            f"against a remaining budget of {self.budget}"
        )


def drive(
    arm: Arm,
    world: World,
    calendar: ComplianceCalendar,
    *,
    failures: Sequence[SyntheticFailure] | None = None,
    days: int | None = None,
    start: dt.datetime | None = None,
) -> Iterator[tuple[CaseView, Proposal]]:
    """Run an arm over a batch and yield every proposal it makes.

    Deliberately does not execute anything. Proposals are collected against an
    unchanging view, so what is measured is what the arm *asked for* -- an arm
    cannot look compliant because the environment happened to reject it.
    """
    batch = list(failures) if failures is not None else generate_batch(world)
    start = start or dt.datetime(2026, 3, 2, 1, 0, tzinfo=IST)
    horizon = days if days is not None else world.horizon_days

    views = {
        failure.case_id: make_case_view(
            case_id=failure.case_id,
            rail=failure.rail,
            amount_paise=failure.amount_paise,
            failed_at=failure.failed_at,
            observed=failure.observed(),
        )
        for failure in batch
    }

    for day in range(horizon):
        now = start + dt.timedelta(days=day)
        for case_id in list(views):
            view = views[case_id]
            # Called exactly once per case per day: an arm may be stateful, and
            # a harness that polls it twice would measure something the
            # simulator never does.
            proposals = list(arm.propose(view, now))
            for proposal in proposals:
                yield view, proposal

            executions = sum(
                1 for proposal in proposals if proposal.kind is ActionKind.ATTEMPT
            )
            contacts = sum(
                1 for proposal in proposals if proposal.kind is ActionKind.CONTACT
            )
            if executions or contacts:
                # Reflect the spend so the next day's view is honest rather than
                # replaying a fresh case. Only executions move `attempts_used` --
                # a contact is not a mandate execution.
                views[case_id] = make_case_view(
                    case_id=view.case_id,
                    rail=view.rail,
                    amount_paise=view.amount_paise,
                    failed_at=view.failed_at,
                    observed=view.observed,
                    attempts_used=view.attempts_used + executions,
                    contacts_used=view.contacts_used + contacts,
                )


def execution_budget_violations(
    arm: Arm,
    world: World,
    calendar: ComplianceCalendar,
    *,
    failures: Sequence[SyntheticFailure] | None = None,
    days: int | None = None,
) -> list[BudgetViolation]:
    """Chains where the arm proposed more executions than the cap allows.

    Counts ATTEMPT proposals only. A contact is not a mandate execution and
    does not consume the NPCI budget.
    """
    budget = remaining_execution_budget(calendar)
    proposed: dict[str, int] = {}
    for view, proposal in drive(arm, world, calendar, failures=failures, days=days):
        if proposal.kind is ActionKind.ATTEMPT:
            proposed[view.case_id] = proposed.get(view.case_id, 0) + 1

    return [
        BudgetViolation(case_id=case_id, executions_proposed=count, budget=budget)
        for case_id, count in sorted(proposed.items())
        if count > budget
    ]


def assert_within_execution_budget(
    arm: Arm,
    world: World,
    calendar: ComplianceCalendar,
    *,
    failures: Sequence[SyntheticFailure] | None = None,
    days: int | None = None,
) -> None:
    violations = execution_budget_violations(
        arm, world, calendar, failures=failures, days=days
    )
    if violations:
        shown = "\n  ".join(str(violation) for violation in violations[:5])
        raise AssertionError(
            f"{len(violations)} chain(s) exceeded the mandate-execution budget:\n  {shown}"
        )


# --------------------------------------------------------------------------- #
# Ordinal-only static check
# --------------------------------------------------------------------------- #

# Names that only exist to carry a magnitude. Reading one from policy code means
# the policy is depending on a cardinal value.
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "true_class",
        "emission_faithful",
        "link_conversion",
        "revocation_hazard",
        "revocation_per_notification",
        "recovery_curve",
        "fatigue_multiplier",
        "emission_fidelity",
        # Removed from the world in favour of an explicit horizon sweep. The
        # name stays on the forbidden list so reintroducing it as a policy input
        # is still caught.
        "remaining_lifetime_months",
    }
)

FORBIDDEN_IMPORTS = frozenset({"World", "RecoveryCurve", "sample_world", "SyntheticFailure"})

SUPPRESSION = "ordinal-ok"


@dataclass(frozen=True)
class OrdinalViolation:
    path: str
    line: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.kind}] {self.detail}"


class _OrdinalVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source_lines: Sequence[str]) -> None:
        self.path = path
        self.lines = source_lines
        self.violations: list[OrdinalViolation] = []

    def _suppressed(self, line: int) -> bool:
        if 1 <= line <= len(self.lines):
            return SUPPRESSION in self.lines[line - 1]
        return False

    def _record(self, node: ast.AST, kind: str, detail: str) -> None:
        line = getattr(node, "lineno", 0)
        if self._suppressed(line):
            return
        self.violations.append(
            OrdinalViolation(path=self.path, line=line, kind=kind, detail=detail)
        )

    def visit_Constant(self, node: ast.Constant) -> None:
        # A float strictly inside (0, 1) in policy code is a probability.
        # Integers are fine: attempt counts and day offsets are ordinal.
        if isinstance(node.value, float) and 0.0 < node.value < 1.0:
            self._record(
                node,
                "probability-literal",
                f"float {node.value} in (0,1) reads as a probability. Policy may "
                f"depend on orderings, not magnitudes. Suppress with # {SUPPRESSION}: <why>",
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRIBUTES:
            self._record(
                node,
                "ground-truth-access",
                f".{node.attr} is simulator ground truth or a swept magnitude; "
                "the allocator must not read it",
            )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # Branching on a raw confidence value rather than on the band.
        for side in [node.left, *node.comparators]:
            if isinstance(side, ast.Attribute) and side.attr == "confidence":
                self._record(
                    node,
                    "confidence-threshold",
                    "comparing .confidence directly. Branch on .band instead -- the "
                    "thresholds are config, not policy",
                )
                break
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in FORBIDDEN_IMPORTS:
                self._record(
                    node,
                    "forbidden-import",
                    f"{alias.name} carries simulator ground truth into the policy path",
                )
        self.generic_visit(node)


def ordinal_violations(paths: Iterable[pathlib.Path | str]) -> list[OrdinalViolation]:
    """Static check that policy code reasons about orderings, not magnitudes.

    A static check rather than a runtime one because the failure is a *shape* of
    reasoning, not an event: an allocator that hardcodes `0.41` never raises,
    it just quietly encodes a number nobody can defend. There is nothing to
    observe at runtime, so the source is what gets inspected.

    Escape hatch: put `# ordinal-ok: <reason>` on the line. Deliberately visible
    and greppable -- a suppression should be a decision someone can find, not a
    silent exception.
    """
    violations: list[OrdinalViolation] = []
    for raw_path in paths:
        path = pathlib.Path(raw_path)
        if not path.is_file() or path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        visitor = _OrdinalVisitor(path.as_posix(), source.splitlines())
        visitor.visit(ast.parse(source, filename=str(path)))
        violations.extend(visitor.violations)
    return violations


def allocator_modules(root: pathlib.Path | str = "allocator") -> list[pathlib.Path]:
    root = pathlib.Path(root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
