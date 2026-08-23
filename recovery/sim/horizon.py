"""Horizon sensitivity: at what remaining lifetime does preservation win?

Arm B recovers more money this cycle by contacting nearly everyone, and pays
for it in mandates. Arm C is deliberately more conservative. Comparing them on
one cycle's recovery answers a question nobody asked -- a mandate is an annuity,
and an arm that recovers less now while keeping more mandates alive is ahead
from some horizon onward.

So the question is not *which recovers more this cycle*. It is **at what
remaining lifetime does mandate preservation outweigh cycle recovery**, and how
much that answer moves with the revocation hazard.

## Reporting discipline

The output is a **crossover horizon**, reported as a band across the swept
hazard range. Never a single lifetime at a single hazard, and never a headline
rupee figure -- that would be the LTV point estimate the whole project refuses
to make. Where the ordering does not change inside the swept range, that is the
finding and it is reported as such.

## The one derived quantity

`monthly_value` is the batch's mean subscription charge, taken from the world
rather than assumed: it is `money_at_risk / cases`. It is not a new parameter
and it is not an LTV estimate. Multiplying it by a lifetime produces a value
*ordering*, and only the lifetime at which that ordering flips is reported.

## Two bases, because "preserved" is ambiguous -- and one of them is degenerate

`NOT_REVOKED` counts every mandate that was not revoked, including halted
subscriptions: the mandate survives but needs manual intervention to bill.

`WORKING` counts only those neither revoked nor halted -- and **under the
current outcome model it collapses to cycle recovery**. Every case ends
recovered, revoked, or halted, so `preserved - halted` is identically
`cases_recovered`. The annuity term becomes proportional to the cycle term and
carries no independent information.

That is a defect in the outcome model, not a result. It is kept and reported
because the degeneracy is worth seeing: a "no crossover under WORKING" line is
not evidence against an arm, it is the basis measuring the same thing twice.
Making it a real sensitivity needs a modelled rate at which halted
subscriptions are manually recovered, and nobody publishes one -- so the
alternative to reporting the degeneracy is inventing a parameter.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Sequence

from recovery.classifier import CostModel
from recovery.sim.arms import Arm
from recovery.sim.calendar import ComplianceCalendar
from recovery.sim.metrics import ArmMetrics
from recovery.sim.run import run_comparison
from recovery.sim.world import World

DEFAULT_LIFETIMES = tuple(range(1, 25))


class SurvivalBasis(str, enum.Enum):
    """How to count a preserved mandate.

    NOT_REVOKED is the meaningful one. WORKING is degenerate under the current
    three-state outcome model -- see the module docstring -- and is retained so
    the degeneracy is visible rather than silently absent.
    """

    NOT_REVOKED = "not_revoked"
    WORKING = "working"

    @property
    def is_degenerate(self) -> bool:
        """True where the basis collapses into the cycle-recovery term."""
        return self is SurvivalBasis.WORKING

    def count(self, metrics: ArmMetrics) -> int:
        if self is SurvivalBasis.NOT_REVOKED:
            return metrics.mandates_preserved
        return max(0, metrics.mandates_preserved - metrics.mandates_halted)


@dataclass(frozen=True)
class Crossover:
    """Where one arm overtakes another, in months of remaining lifetime."""

    challenger: str
    incumbent: str
    hazard: float
    # None when the ordering never flips inside any horizon: the challenger
    # neither recovers more this cycle nor preserves more mandates.
    months: float | None
    already_ahead: bool

    def describe(self) -> str:
        if self.already_ahead:
            return f"{self.challenger} is ahead of {self.incumbent} at any horizon"
        if self.months is None:
            return f"{self.challenger} never overtakes {self.incumbent}"
        return f"{self.challenger} overtakes {self.incumbent} at {self.months:.1f} months"


@dataclass(frozen=True)
class CrossoverBand:
    """A crossover reported across the hazard range, never at one point."""

    challenger: str
    incumbent: str
    basis: SurvivalBasis
    crossovers: tuple[Crossover, ...]

    @property
    def months(self) -> tuple[float, ...]:
        return tuple(c.months for c in self.crossovers if c.months is not None)

    @property
    def never_count(self) -> int:
        return sum(1 for c in self.crossovers if c.months is None and not c.already_ahead)

    @property
    def always_count(self) -> int:
        return sum(1 for c in self.crossovers if c.already_ahead)

    def describe(self) -> str:
        total = len(self.crossovers)
        if self.always_count == total:
            return (
                f"{self.challenger} is ahead of {self.incumbent} at every hazard, "
                "at any horizon"
            )
        if self.never_count == total:
            return (
                f"{self.challenger} never overtakes {self.incumbent} at any hazard "
                "in the swept range"
            )
        months = self.months
        span = (
            f"{min(months):.1f}-{max(months):.1f} months"
            if len(months) > 1 and min(months) != max(months)
            else f"{months[0]:.1f} months"
        )
        caveat = ""
        if self.never_count:
            caveat = f"; never overtakes at {self.never_count} of {total} hazard points"
        if self.always_count:
            caveat += f"; already ahead at {self.always_count} of {total}"
        return (
            f"{self.challenger} overtakes {self.incumbent} at {span} of remaining "
            f"lifetime across the swept hazard range{caveat}"
        )


@dataclass(frozen=True)
class HorizonSweep:
    hazard_points: tuple[float, ...]
    lifetimes: tuple[int, ...]
    basis: SurvivalBasis
    monthly_value_paise: float
    # arm -> hazard index -> (cycle_recovery_paise, preserved_count)
    cycle: Mapping[str, tuple[int, ...]]
    preserved: Mapping[str, tuple[int, ...]]

    def value_paise(self, arm: str, hazard_index: int, lifetime_months: int) -> float:
        return (
            self.cycle[arm][hazard_index]
            + self.preserved[arm][hazard_index] * self.monthly_value_paise * lifetime_months
        )

    def value_band_inr(self, arm: str, lifetime_months: int) -> tuple[float, float]:
        """Min and max across the hazard range. Never one hazard point."""
        values = [
            self.value_paise(arm, index, lifetime_months) / 100
            for index in range(len(self.hazard_points))
        ]
        return (min(values), max(values))

    def crossover(self, challenger: str, incumbent: str) -> CrossoverBand:
        """Analytic, because value is linear in lifetime.

        value(arm, L) = cycle + preserved * monthly * L, so the arms cross where
        L = (cycle_incumbent - cycle_challenger)
            / ((preserved_challenger - preserved_incumbent) * monthly)
        """
        crossovers = []
        for index, hazard in enumerate(self.hazard_points):
            cycle_gap = self.cycle[incumbent][index] - self.cycle[challenger][index]
            preserved_gap = (
                self.preserved[challenger][index] - self.preserved[incumbent][index]
            )

            if cycle_gap <= 0 and preserved_gap >= 0:
                crossovers.append(
                    Crossover(challenger, incumbent, hazard, months=0.0, already_ahead=True)
                )
                continue
            if preserved_gap <= 0:
                # Recovers less and preserves no more: no horizon rescues it.
                crossovers.append(
                    Crossover(challenger, incumbent, hazard, months=None, already_ahead=False)
                )
                continue

            months = cycle_gap / (preserved_gap * self.monthly_value_paise)
            crossovers.append(
                Crossover(challenger, incumbent, hazard, months=months, already_ahead=False)
            )

        return CrossoverBand(
            challenger=challenger,
            incumbent=incumbent,
            basis=self.basis,
            crossovers=tuple(crossovers),
        )


def horizon_sweep(
    arms: Sequence[Arm],
    world: World,
    calendar: ComplianceCalendar,
    hazard_range: tuple[float, float],
    *,
    hazard_points: int = 5,
    lifetimes: Sequence[int] = DEFAULT_LIFETIMES,
    basis: SurvivalBasis = SurvivalBasis.NOT_REVOKED,
    costs: CostModel | None = None,
) -> HorizonSweep:
    """Run each arm once per hazard point; lifetimes are then analytic.

    Lifetime does not change the simulation -- it only scales the annuity term --
    so the sweep costs one comparison per hazard rather than one per (hazard,
    lifetime) pair.
    """
    low, high = hazard_range
    step = (high - low) / (hazard_points - 1) if hazard_points > 1 else 0.0
    points = tuple(low + step * index for index in range(hazard_points))

    names = [arm.name for arm in arms]
    cycle: dict[str, list[int]] = {name: [] for name in names}
    preserved: dict[str, list[int]] = {name: [] for name in names}
    monthly_values: list[float] = []

    for hazard in points:
        result = run_comparison(arms, world.with_mandate_hazard(hazard), calendar, costs=costs)
        for name in names:
            metrics = result.metrics[name]
            cycle[name].append(metrics.money_recovered_paise)
            preserved[name].append(basis.count(metrics))
        reference = result.metrics[names[0]]
        if reference.cases:
            monthly_values.append(reference.money_at_risk_paise / reference.cases)

    return HorizonSweep(
        hazard_points=points,
        lifetimes=tuple(lifetimes),
        basis=basis,
        monthly_value_paise=sum(monthly_values) / len(monthly_values) if monthly_values else 0.0,
        cycle={name: tuple(values) for name, values in cycle.items()},
        preserved={name: tuple(values) for name, values in preserved.items()},
    )
