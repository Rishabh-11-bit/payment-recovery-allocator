"""C8 -- robustness sweep. Where does the result break, and on what?

A single world proves nothing: it is one draw from ranges that are mostly
unsourced. So the whole world space is sampled and every arm is run in every
world.

## What is reported, and what deliberately is not

**Not a win rate on its own.** "C wins in 87% of worlds" invites the reply "so
you picked the 87%", and it is unfalsifiable without knowing what the other 13%
look like. The output is the *breaking point*: the parameter conditions under
which C loses, named, with the loss rate on each side of the split.

**The crossover as a distribution.** The horizon crossover was previously swept
over the revocation hazard alone. Here every world contributes one crossover, so
the band comes from the full parameter space rather than from one axis of it.

**Two range sets.** `nominal` samples inside the calibrated ranges and answers
"does it hold where we think we are". `stress` widens the parameters most likely
to invert the result and answers "where does it break" -- a question the nominal
sweep cannot reach, because the break may lie outside the calibrated ranges
entirely. Stress figures are never reported as results; they locate the edge.

## How conditions are named

For each parameter, the split point that best separates worlds where C wins from
worlds where it loses, by loss rate. A decision stump, deliberately: it produces
a statement a person can check -- "C loses when TERMINAL link conversion is above
0.14, at a 71% loss rate on that side against 4% below it" -- rather than a
coefficient nobody can argue with.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from recovery.classifier import CostModel
from recovery.models import FailureClass
from recovery.sim.arms import Arm
from recovery.sim.calendar import ComplianceCalendar
from recovery.sim.horizon import SurvivalBasis
from recovery.sim.run import run_comparison
from recovery.sim.world import World, sample_world

# A world where C only overtakes past this many months is treated as a loss:
# beyond it the claim depends on subscription lifetimes we cannot evidence.
DEFAULT_WIN_HORIZON_MONTHS = 12.0

# A split must leave at least this many worlds on each side to be reported.
# Without it the "condition" is one outlier with a decimal point.
MIN_BUCKET = 15


def stress_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge the `stress:` block over the calibrated ranges."""
    merged = copy.deepcopy(dict(raw))
    overrides = merged.pop("stress", None) or {}

    def merge(target: dict, source: Mapping) -> None:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = value

    merge(merged, overrides)
    return merged


def world_parameters(world: World) -> dict[str, float]:
    """The cardinal values of one world, flattened for attribution.

    Only sampled quantities. Anything constant across worlds cannot explain a
    difference between worlds and would be noise in the split search.
    """
    params: dict[str, float] = {
        "revocation_per_notification": world.revocation_per_notification,
        "fatigue_multiplier": world.fatigue_multiplier,
        "emission_fidelity": world.emission_fidelity,
    }
    for failure_class in FailureClass:
        name = failure_class.value
        curve = world.recovery[failure_class]
        if failure_class is not FailureClass.TERMINAL:
            params[f"recovery_{name}_base"] = curve.base
            params[f"recovery_{name}_per_day"] = curve.per_day
            params[f"recovery_{name}_cap"] = curve.cap
        params[f"link_conversion_{name}"] = world.link_conversion[failure_class]
        params[f"class_mix_{name}"] = world.class_mix.get(name, 0.0)
    for rail, share in world.rail_mix.items():
        params[f"rail_mix_{rail}"] = share
    return params


@dataclass(frozen=True)
class WorldOutcome:
    seed: int
    parameters: Mapping[str, float]
    cycle_paise: Mapping[str, int]
    preserved: Mapping[str, int]
    monthly_value_paise: float
    # incumbent -> months until C overtakes. None means never.
    crossover: Mapping[str, float | None]
    already_ahead: Mapping[str, bool]

    def wins_against(self, incumbent: str, horizon: float) -> bool:
        if self.already_ahead.get(incumbent):
            return True
        months = self.crossover.get(incumbent)
        return months is not None and months <= horizon

    def cycle_winner(self) -> str:
        return max(self.cycle_paise, key=lambda name: self.cycle_paise[name])


def _crossover_months(
    cycle_challenger: int,
    cycle_incumbent: int,
    preserved_challenger: int,
    preserved_incumbent: int,
    monthly_value: float,
) -> tuple[float | None, bool]:
    """Analytic. Value is linear in lifetime, so the crossing is exact."""
    cycle_gap = cycle_incumbent - cycle_challenger
    preserved_gap = preserved_challenger - preserved_incumbent
    if cycle_gap <= 0 and preserved_gap >= 0:
        return 0.0, True
    if preserved_gap <= 0 or monthly_value <= 0:
        return None, False
    return cycle_gap / (preserved_gap * monthly_value), False


def sweep(
    arms: Sequence[Arm],
    calendar: ComplianceCalendar,
    raw_config: Mapping[str, Any],
    *,
    worlds: int = 300,
    first_seed: int = 1,
    challenger: str = "C",
    incumbents: Sequence[str] = ("A", "B"),
    basis: SurvivalBasis = SurvivalBasis.NOT_REVOKED,
    costs: CostModel | None = None,
) -> list[WorldOutcome]:
    """One comparison per sampled world. Deterministic for a seed range."""
    outcomes: list[WorldOutcome] = []
    for offset in range(worlds):
        seed = first_seed + offset
        world = sample_world(seed=seed, raw=raw_config)
        result = run_comparison(arms, world, calendar, costs=costs)

        cycle = {n: m.money_recovered_paise for n, m in result.metrics.items()}
        preserved = {n: basis.count(m) for n, m in result.metrics.items()}
        reference = next(iter(result.metrics.values()))
        monthly = (
            reference.money_at_risk_paise / reference.cases if reference.cases else 0.0
        )

        crossover: dict[str, float | None] = {}
        ahead: dict[str, bool] = {}
        for incumbent in incumbents:
            months, already = _crossover_months(
                cycle[challenger],
                cycle[incumbent],
                preserved[challenger],
                preserved[incumbent],
                monthly,
            )
            crossover[incumbent] = months
            ahead[incumbent] = already

        outcomes.append(
            WorldOutcome(
                seed=seed,
                parameters=world_parameters(world),
                cycle_paise=cycle,
                preserved=preserved,
                monthly_value_paise=monthly,
                crossover=crossover,
                already_ahead=ahead,
            )
        )
    return outcomes


# --------------------------------------------------------------------------- #
# Breaking points
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BreakingPoint:
    parameter: str
    threshold: float
    direction: str  # "above" or "below"
    loss_rate_inside: float
    loss_rate_outside: float
    worlds_inside: int
    worlds_outside: int

    @property
    def separation(self) -> float:
        return self.loss_rate_inside - self.loss_rate_outside

    def describe(self) -> str:
        return (
            f"{self.parameter} {self.direction} {self.threshold:.4g}: "
            f"loses {self.loss_rate_inside:.0%} of {self.worlds_inside} worlds, "
            f"against {self.loss_rate_outside:.0%} of {self.worlds_outside} on the "
            "other side"
        )


def breaking_points(
    outcomes: Sequence[WorldOutcome],
    incumbent: str,
    *,
    horizon: float = DEFAULT_WIN_HORIZON_MONTHS,
    min_separation: float = 0.20,
    top: int = 6,
) -> list[BreakingPoint]:
    """Name the parameter conditions under which the challenger loses.

    A decision stump per parameter: the split maximising the difference in loss
    rate between the two sides. Reported as a condition a reader can check
    against their own beliefs, not as a coefficient.
    """
    if not outcomes:
        return []
    losses = [not outcome.wins_against(incumbent, horizon) for outcome in outcomes]
    if not any(losses) or all(losses):
        return []

    found: list[BreakingPoint] = []
    for parameter in outcomes[0].parameters:
        values = [outcome.parameters[parameter] for outcome in outcomes]
        best: BreakingPoint | None = None
        # Candidate splits at deciles: enough resolution to locate an edge,
        # few enough that the split is not fitted to individual worlds.
        ordered = sorted(values)
        candidates = {
            ordered[int(len(ordered) * fraction)]
            for fraction in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
        }
        for threshold in candidates:
            for direction in ("above", "below"):
                if direction == "above":
                    inside = [i for i, v in enumerate(values) if v > threshold]
                else:
                    inside = [i for i, v in enumerate(values) if v < threshold]
                outside = [i for i in range(len(values)) if i not in set(inside)]
                if len(inside) < MIN_BUCKET or len(outside) < MIN_BUCKET:
                    continue
                rate_in = sum(losses[i] for i in inside) / len(inside)
                rate_out = sum(losses[i] for i in outside) / len(outside)
                point = BreakingPoint(
                    parameter=parameter,
                    threshold=threshold,
                    direction=direction,
                    loss_rate_inside=rate_in,
                    loss_rate_outside=rate_out,
                    worlds_inside=len(inside),
                    worlds_outside=len(outside),
                )
                if best is None or point.separation > best.separation:
                    best = point
        if best is not None and best.separation >= min_separation:
            found.append(best)

    return sorted(found, key=lambda point: -point.separation)[:top]


# --------------------------------------------------------------------------- #
# Distributions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CrossoverDistribution:
    incumbent: str
    months: tuple[float, ...]
    never: int
    already_ahead: int
    total: int

    def percentile(self, fraction: float) -> float | None:
        if not self.months:
            return None
        ordered = sorted(self.months)
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    def win_rate(self, horizon: float) -> float:
        if not self.total:
            return 0.0
        wins = self.already_ahead + sum(1 for m in self.months if m <= horizon)
        return wins / self.total

    def describe(self) -> str:
        if not self.months and not self.already_ahead:
            return f"C never overtakes {self.incumbent} in any of {self.total} worlds"
        parts = [
            f"vs {self.incumbent} over {self.total} worlds",
            f"ahead from the start in {self.already_ahead}",
        ]
        if self.months:
            parts.append(
                f"crossover p10 {self.percentile(0.1):.1f}mo / "
                f"median {self.percentile(0.5):.1f}mo / "
                f"p90 {self.percentile(0.9):.1f}mo"
            )
        if self.never:
            parts.append(f"never overtakes in {self.never}")
        return "; ".join(parts)


def crossover_distribution(
    outcomes: Sequence[WorldOutcome], incumbent: str
) -> CrossoverDistribution:
    months: list[float] = []
    never = ahead = 0
    for outcome in outcomes:
        if outcome.already_ahead.get(incumbent):
            ahead += 1
            continue
        value = outcome.crossover.get(incumbent)
        if value is None:
            never += 1
        else:
            months.append(value)
    return CrossoverDistribution(
        incumbent=incumbent,
        months=tuple(months),
        never=never,
        already_ahead=ahead,
        total=len(outcomes),
    )


@dataclass
class SweepReport:
    label: str
    worlds: int
    outcomes: list[WorldOutcome] = field(default_factory=list)

    def distribution(self, incumbent: str) -> CrossoverDistribution:
        return crossover_distribution(self.outcomes, incumbent)

    def breaking(self, incumbent: str, **kwargs) -> list[BreakingPoint]:
        return breaking_points(self.outcomes, incumbent, **kwargs)

    def cycle_win_share(self, arm: str) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.cycle_winner() == arm) / len(self.outcomes)
