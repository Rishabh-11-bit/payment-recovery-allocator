"""C2 machinery: lookup-table loader, confidence ladder, cost-based fallback.

**This module contains no taxonomy.** The `(method, source, step, reason)` ->
class mapping and the cost matrix live in `config/classifier.yaml` and are
hand-authored. What is here is the machinery that reads them, and it refuses to
run against a file still marked `status: STUB` unless a caller explicitly opts
in -- so the stub cannot reach a result by accident.

Three behaviours are worth stating plainly, because they are what the panel
will push on:

* **Nothing is silently defaulted.** A key that matches no rule gets the
  configured fallback, `mapped=False`, and a `failure.unmapped` audit event
  carrying the key that missed. The fallback is never indistinguishable from a
  real classification.
* **Confidence is an output.** It comes from the matched rule, is banded by
  configured thresholds, and the band is what downstream branches on. A
  MODERATE band permits reordering but not exclusion, because excluding on a
  misdiagnosis makes recovery harder.
* **Low confidence resolves toward the cheaper error.** Not toward the more
  likely class. A LOW band discards the predicted class and asks the cost
  matrix which class has the lowest worst-case cost of being wrong. That is a
  minimax choice, and it is deliberate: under low confidence we are not
  estimating what happened, we are limiting what it costs to be wrong.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import yaml

from recovery.models import (
    Classification,
    ConfidenceBand,
    FailureClass,
    NormalizedFailure,
)

DEFAULT_CLASSIFIER_PATH = pathlib.Path("config/classifier.yaml")
KEY_FIELDS = ("method", "source", "step", "reason")

# Most discriminating first. This is the order rule precedence is decided in,
# and it is not the order the key is written in.
#
# `reason` names the cause outright. `step` localises where in the flow it
# broke, and CLAUDE.md already states that step localises better than source.
# `source` names which party reported it. `method` is the rail, which narrows
# the value space and says nothing about what happened.
#
# Counting named fields instead -- which is what this file did until the
# mandate rows were added -- treats all four as equally informative, so a rule
# naming only `reason` loses to one naming `method` and `step`. That is
# backwards, and silently: the losing rule is present, valid, and never fires.
FIELDS_BY_INFORMATIVENESS = ("reason", "step", "source", "method")


class ClassifierConfigError(ValueError):
    """The mapping file is unusable. Always raised at load, never at classify."""


@dataclass(frozen=True)
class Rule:
    index: int
    match: Mapping[str, str]
    failure_class: FailureClass
    confidence: float
    note: str | None = None
    # Optional sub-classification. Two failures can share a class and still need
    # different actions -- see Classification.cause_family.
    cause_family: str | None = None
    # Declared low on purpose. Validated at load: a rule claiming this while
    # sitting above the moderate threshold is a contradiction, not a nuance.
    deliberately_low_confidence: bool = False
    # No event this system ingests can produce this key. The row is taxonomy
    # completeness, not dead weight -- but the difference between "we have an
    # answer for this" and "this ever arrives" is worth being able to read off
    # the config rather than inferring from the ingest filter.
    unreachable: bool = False

    @property
    def specificity(self) -> int:
        """Named-field count. Reported in errors; no longer decides precedence."""
        return len(self.match)

    @property
    def precedence(self) -> tuple[int, ...]:
        """Which rule wins when two match. Compared lexicographically, high first.

        Field *identity* dominates field *count*: a rule naming `reason` alone
        beats one naming `method` and `step`, because it is making the more
        specific claim about the failure even though it makes fewer of them.
        Among rules that name the same discriminating field, naming more of the
        rest still wins -- the tuple is compared position by position.
        """
        return tuple(int(field in self.match) for field in FIELDS_BY_INFORMATIVENESS)

    def matches(self, key: NormalizedFailure) -> bool:
        """An omitted field is a wildcard. A named field must equal the key's."""
        for field, expected in self.match.items():
            if getattr(key, field) != expected:
                return False
        return True

    def conflicts_with(self, other: Rule) -> bool:
        """True if some key would match both at the same precedence.

        Two rules are compatible when every field they both name agrees; a key
        built from the union of their constraints then matches both. Equal
        precedence means neither wins, so it would be a coin toss.
        """
        if self.precedence != other.precedence:
            return False
        for field in set(self.match) & set(other.match):
            if self.match[field] != other.match[field]:
                return False
        return True

    @property
    def outcome(self) -> tuple[FailureClass, float]:
        return (self.failure_class, self.confidence)


@dataclass(frozen=True)
class CostModel:
    """What being wrong costs, and what contacting the customer costs.

    Two priced things, because a decision costs more than a wrong label. The
    misclassification matrix prices getting the class wrong; `contact` prices
    one customer-visible contact against the failure's true class, which is
    where the mandate-survival argument enters the arithmetic.

    Ordinal -- only comparisons are read, never magnitudes.
    """

    misclassification: Mapping[FailureClass, Mapping[FailureClass, float]]
    contact: Mapping[FailureClass, float]

    def cost(self, true_class: FailureClass, predicted: FailureClass) -> float:
        return self.misclassification[true_class][predicted]

    def contact_cost(self, true_class: FailureClass) -> float:
        return self.contact[true_class]

    def worst_case(self, predicted: FailureClass) -> float:
        """The most this prediction can cost, over every class it might really be."""
        return max(self.cost(true, predicted) for true in FailureClass)

    def safest_class(self, candidates: Iterable[FailureClass] | None = None) -> FailureClass:
        """The class with the lowest worst-case cost of being wrong.

        This is what a LOW confidence band resolves to. Ties break on total cost,
        then on name, so the result is deterministic rather than dict-ordered.
        """
        options = list(candidates) if candidates is not None else list(FailureClass)
        return min(
            options,
            key=lambda option: (
                self.worst_case(option),
                sum(self.cost(true, option) for true in FailureClass),
                option.value,
            ),
        )


@dataclass(frozen=True)
class ClassifierConfig:
    status: str
    version: str
    source_space: Mapping[str, frozenset[str]]
    step_space: Mapping[str, frozenset[str]]
    source_aliases: Mapping[str, str]
    high_threshold: float
    moderate_threshold: float
    fallback_confidence: float
    costs: CostModel
    rules: tuple[Rule, ...]

    @property
    def is_stub(self) -> bool:
        return self.status.upper() == "STUB"

    @property
    def unreachable_rules(self) -> tuple[Rule, ...]:
        """Rows no ingested event can currently produce. See `Rule.unreachable`."""
        return tuple(rule for rule in self.rules if rule.unreachable)


class Classifier:
    """Deterministic lookup over the configured table. No model, no LLM."""

    def __init__(self, config: ClassifierConfig) -> None:
        self.config = config
        # Most discriminating first; declaration order breaks equal precedence,
        # and load-time validation has already ruled out ambiguous ties.
        self._rules = sorted(
            config.rules,
            key=lambda rule: (tuple(-part for part in rule.precedence), rule.index),
        )

    def band_for(self, confidence: float) -> ConfidenceBand:
        if confidence >= self.config.high_threshold:
            return ConfidenceBand.HIGH
        if confidence >= self.config.moderate_threshold:
            return ConfidenceBand.MODERATE
        return ConfidenceBand.LOW

    def classify(self, key: NormalizedFailure) -> Classification:
        """Match the table. An undocumented source is surfaced, never rejected.

        This module previously routed a source outside its method's documented
        set straight to the fallback. That was wrong: the documented value space
        is a lower bound, and the rejection discarded ordinary production
        payloads -- a real netbanking failure returns `source: bank`, which the
        reference lists only for emandate. The flag now rides along on the
        result for review, and classification proceeds normally. CHALLENGES 007.
        """
        undocumented = not key.source_in_documented_space

        for rule in self._rules:
            if rule.matches(key):
                return self._from_rule(rule, key, undocumented=undocumented)

        return self._fallback(key, undocumented=undocumented)

    def _from_rule(
        self, rule: Rule, key: NormalizedFailure, *, undocumented: bool = False
    ) -> Classification:
        band = self.band_for(rule.confidence)
        common = {
            "confidence": rule.confidence,
            "band": band,
            "key": key,
            "mapped": True,
            "rule_index": rule.index,
            "note": rule.note,
            "source_undocumented": undocumented,
            "cause_family": rule.cause_family,
            "deliberately_low_confidence": rule.deliberately_low_confidence,
        }
        if band is ConfidenceBand.LOW:
            return Classification(
                failure_class=self.config.costs.safest_class(),
                cost_resolved_from=rule.failure_class,
                **common,
            )
        return Classification(failure_class=rule.failure_class, **common)

    def _fallback(
        self, key: NormalizedFailure, *, undocumented: bool = False
    ) -> Classification:
        """Unmapped. Resolved through the cost model.

        There is no configured fallback class: a safe default written down
        separately from the matrix that justifies it is a default that will
        eventually disagree with it. Deriving it means changing the costs
        changes the fallback, automatically and visibly.

        Never silent -- the caller audits on `mapped is False`.
        """
        confidence = self.config.fallback_confidence
        return Classification(
            failure_class=self.config.costs.safest_class(),
            confidence=confidence,
            band=self.band_for(confidence),
            key=key,
            mapped=False,
            rule_index=None,
            source_undocumented=undocumented,
        )


# ------------------------------------------------------------------ loading --


def _parse_rules(raw: Any) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        raise ClassifierConfigError("`rules` must be a list")
    rules: list[Rule] = []
    for index, item in enumerate(raw):
        match = item.get("match") or {}
        unknown = set(match) - set(KEY_FIELDS)
        if unknown:
            raise ClassifierConfigError(
                f"rule {index} matches on unknown field(s) {sorted(unknown)}; "
                f"the key is {KEY_FIELDS}"
            )
        if not match:
            raise ClassifierConfigError(
                f"rule {index} has an empty match and would catch everything; "
                "use `fallback` for that"
            )
        try:
            failure_class = FailureClass(item["class"])
        except (KeyError, ValueError) as exc:
            raise ClassifierConfigError(f"rule {index}: bad or missing class") from exc
        confidence = float(item.get("confidence", 0.0))
        if not 0.0 <= confidence <= 1.0:
            raise ClassifierConfigError(f"rule {index}: confidence {confidence} outside [0,1]")
        cause_family = item.get("cause_family")
        if cause_family is not None:
            cause_family = str(cause_family).strip().lower()
            if not cause_family:
                raise ClassifierConfigError(
                    f"rule {index}: cause_family is present but empty"
                )
        rules.append(
            Rule(
                index=index,
                match={field: str(value).strip().lower() for field, value in match.items()},
                failure_class=failure_class,
                confidence=confidence,
                note=item.get("note"),
                cause_family=cause_family,
                deliberately_low_confidence=bool(item.get("deliberately_low_confidence")),
                unreachable=bool(item.get("unreachable")),
            )
        )
    _reject_ambiguous(rules)
    return tuple(rules)


def _reject_contradictory_low_confidence(rules: tuple[Rule, ...], moderate: float) -> None:
    """A rule cannot claim deliberate low confidence while banding above LOW."""
    for rule in rules:
        if rule.deliberately_low_confidence and rule.confidence >= moderate:
            raise ClassifierConfigError(
                f"rule {rule.index} sets deliberately_low_confidence but its confidence "
                f"{rule.confidence} is at or above the moderate threshold {moderate}. "
                "Lower the confidence or drop the flag."
            )


def _reject_unknown_rule_steps(
    rules: tuple[Rule, ...], step_space: Mapping[str, frozenset[str]]
) -> None:
    """A rule may not name a step this project has never recorded.

    This is the one place the value spaces are used strictly, and the asymmetry
    is deliberate. `source_space` and `step_space` are lower bounds, so a
    *payload* outside them is surfaced and classified anyway -- rejecting it
    discards real production data wherever the documentation is incomplete
    (CHALLENGES 007). A *rule* is the opposite case: it is authored here, and a
    step this file has never seen is far more likely to be a typo than a
    discovery. The failure mode it prevents is silent -- a misspelled step
    matches nothing, so the row is dead while still appearing in the file,
    counted in the rule total, and reading as covered.

    A rule naming no method is checked against the union: it is intended to
    apply wherever the key occurs.
    """
    if not step_space:
        return
    union = frozenset().union(*step_space.values())
    for rule in rules:
        step = rule.match.get("step")
        if step is None:
            continue
        method = rule.match.get("method")
        allowed = step_space.get(method, frozenset()) if method else union
        if step not in allowed:
            scope = f"method {method!r}" if method else "any method"
            raise ClassifierConfigError(
                f"rule {rule.index} matches step {step!r}, which is not in the step "
                f"space for {scope}. Either it is a typo -- in which case the rule "
                f"would have matched nothing, silently -- or the step is real and "
                f"belongs in `step_space`. Both need a decision; neither is a default."
            )


def _reject_ambiguous(rules: list[Rule]) -> None:
    """Equal-specificity overlaps are a config error, not a runtime coin toss."""
    for i, left in enumerate(rules):
        for right in rules[i + 1 :]:
            if left.conflicts_with(right) and left.outcome != right.outcome:
                raise ClassifierConfigError(
                    f"rules {left.index} and {right.index} both match at precedence "
                    f"{left.precedence} but disagree: {left.match} -> "
                    f"{left.failure_class.value} vs {right.match} -> "
                    f"{right.failure_class.value}. Make one more specific."
                )


def _parse_costs(raw: Any) -> CostModel:
    if not isinstance(raw, dict):
        raise ClassifierConfigError("`costs` must be a mapping")
    matrix_raw = raw.get("misclassification")
    if not isinstance(matrix_raw, dict):
        raise ClassifierConfigError("`costs.misclassification` must be a mapping")

    costs: dict[FailureClass, dict[FailureClass, float]] = {}
    for true_name, row in matrix_raw.items():
        try:
            true_class = FailureClass(true_name)
        except ValueError as exc:
            raise ClassifierConfigError(
                f"costs.misclassification: unknown class {true_name!r}"
            ) from exc
        costs[true_class] = {}
        for predicted_name, value in row.items():
            try:
                predicted = FailureClass(predicted_name)
            except ValueError as exc:
                raise ClassifierConfigError(
                    f"costs.misclassification[{true_name}]: unknown class {predicted_name!r}"
                ) from exc
            costs[true_class][predicted] = float(value)

    missing = [c.value for c in FailureClass if c not in costs]
    if missing:
        raise ClassifierConfigError(f"costs.misclassification missing row(s) for {missing}")
    for true_class, row in costs.items():
        absent = [c.value for c in FailureClass if c not in row]
        if absent:
            raise ClassifierConfigError(
                f"costs.misclassification[{true_class.value}] missing column(s) for {absent}"
            )
        if row[true_class] != 0:
            raise ClassifierConfigError(
                f"costs.misclassification[{true_class.value}][{true_class.value}] must be 0 "
                "-- a correct classification cannot carry a cost"
            )

    contact_raw = raw.get("contact")
    if not isinstance(contact_raw, dict):
        raise ClassifierConfigError(
            "`costs.contact` must be a mapping -- a customer contact carries a cost "
            "even when the class is right"
        )
    contact: dict[FailureClass, float] = {}
    for name, value in contact_raw.items():
        try:
            contact[FailureClass(name)] = float(value)
        except ValueError as exc:
            raise ClassifierConfigError(f"costs.contact: unknown class {name!r}") from exc
    absent_contact = [c.value for c in FailureClass if c not in contact]
    if absent_contact:
        raise ClassifierConfigError(f"costs.contact missing entries for {absent_contact}")

    return CostModel(misclassification=costs, contact=contact)


def load_classifier(
    path: pathlib.Path | str = DEFAULT_CLASSIFIER_PATH,
    *,
    allow_stub: bool = False,
) -> Classifier:
    """Load the mapping. Refuses a stub file unless the caller opts in.

    The gate exists so an unfinished taxonomy cannot quietly produce results
    that look like classifications.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"classifier config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ClassifierConfigError(f"{path} is not a mapping")

    status = str(raw.get("status", "")).strip()
    if status.upper() == "STUB" and not allow_stub:
        raise ClassifierConfigError(
            f"{path} is marked `status: STUB` -- the taxonomy is not authored yet. "
            "Pass allow_stub=True to run the machinery against it deliberately."
        )

    bands = raw.get("confidence_bands") or {}
    high, moderate = float(bands.get("high", 0.0)), float(bands.get("moderate", 0.0))
    if not 0.0 <= moderate <= high <= 1.0:
        raise ClassifierConfigError(
            f"confidence bands must satisfy 0 <= moderate ({moderate}) <= high ({high}) <= 1"
        )

    fallback = raw.get("fallback") or {}
    if "class" in fallback:
        raise ClassifierConfigError(
            "`fallback.class` is not configurable: an unmapped key resolves through "
            "`costs.misclassification`, so a separately-written default would be free "
            "to drift out of agreement with the matrix justifying it. Remove the key."
        )

    config = ClassifierConfig(
        status=status,
        version=str(raw.get("version", "")),
        source_space={
            method: frozenset(str(v).strip().lower() for v in values)
            for method, values in (raw.get("source_space") or {}).items()
        },
        step_space={
            method: frozenset(str(v).strip().lower() for v in values)
            for method, values in (raw.get("step_space") or {}).items()
        },
        source_aliases={
            str(k).strip().lower(): str(v).strip().lower()
            for k, v in (raw.get("source_aliases") or {}).items()
        },
        high_threshold=high,
        moderate_threshold=moderate,
        fallback_confidence=float(fallback.get("confidence", 0.0)),
        costs=_parse_costs(raw.get("costs")),
        rules=_parse_rules(raw.get("rules") or []),
    )
    _reject_contradictory_low_confidence(config.rules, config.moderate_threshold)
    _reject_unknown_rule_steps(config.rules, config.step_space)
    return Classifier(config)
