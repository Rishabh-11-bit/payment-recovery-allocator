"""Run one arm, or compare several, over a sampled world.

Every arm gets the same world, the same batch, and the same seeded draws, so a
difference between arms is a difference in policy rather than in luck. The day
loop is reactive rather than a precomputed schedule because Emandate's next
retry is gated on the previous one resolving.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Mapping, Sequence

from recovery.classifier import CostModel
from recovery.sim.arms import Arm
from recovery.sim.batch import SyntheticFailure, generate_batch
from recovery.sim.calendar import IST, ComplianceCalendar
from recovery.sim.environment import Environment
from recovery.sim.metrics import ArmMetrics
from recovery.sim.world import World

# Arms decide early, before the non-peak morning window, so a T+1 attempt has
# room to clear the 24h PDN lead time.
DECISION_TIME = dt.time(1, 0)


@dataclass(frozen=True)
class ComparisonResult:
    world: World
    metrics: Mapping[str, ArmMetrics]

    def row(self, arm: str) -> Mapping[str, object]:
        return self.metrics[arm].as_row()

    def uplift(self, better: str, baseline: str) -> float:
        """Money-recovered difference, in rupees. Positive means `better` won."""
        return (
            self.metrics[better].money_recovered_paise
            - self.metrics[baseline].money_recovered_paise
        ) / 100


def run_arm(
    arm: Arm,
    world: World,
    calendar: ComplianceCalendar,
    *,
    failures: Sequence[SyntheticFailure] | None = None,
    costs: CostModel | None = None,
    start: dt.datetime | None = None,
) -> ArmMetrics:
    batch = list(failures) if failures is not None else generate_batch(world)
    # Fresh copies: an arm must not see another arm's mutations.
    batch = [
        SyntheticFailure(**{**failure.__dict__}) for failure in batch
    ]
    environment = Environment(world, batch, calendar, costs)
    metrics = ArmMetrics(arm=arm.name)

    start = start or dt.datetime.combine(
        min(failure.failed_at for failure in batch).astimezone(IST).date(),
        DECISION_TIME,
        tzinfo=IST,
    )

    for day in range(world.horizon_days):
        now = start + dt.timedelta(days=day)
        for case_id in environment.open_case_ids():
            view = environment.view(case_id, now)
            for proposal in arm.propose(view, now):
                environment.submit(proposal, now, metrics)

    environment.finalise(metrics)
    return metrics


def run_comparison(
    arms: Sequence[Arm],
    world: World,
    calendar: ComplianceCalendar,
    *,
    costs: CostModel | None = None,
) -> ComparisonResult:
    """One batch, every arm. Identical inputs by construction."""
    batch = generate_batch(world)
    return ComparisonResult(
        world=world,
        metrics={
            arm.name: run_arm(arm, world, calendar, failures=batch, costs=costs)
            for arm in arms
        },
    )


# --------------------------------------------------- mandate survival ------


@dataclass(frozen=True)
class DominanceResult:
    """Mandate survival, reported as an ordering rather than a count.

    A count of mandates preserved depends on a per-notification revocation
    hazard, and no such rate is published -- inventing one and quoting the count
    it produces would be a cardinal claim dressed as a result.

    The *ordering* does not need the rate. Sweeping the hazard across its whole
    configured range and observing that one arm preserves more at every point is
    an ordinal claim, and it survives any hazard in the range. Where the ordering
    inverts, that is the finding, and the crossover point is reported instead.
    """

    arms: tuple[str, ...]
    hazard_points: tuple[float, ...]
    # preserved counts per hazard point, per arm. Internal: used to establish the
    # ordering and the crossover. Never a headline figure.
    _counts: Mapping[str, tuple[int, ...]]
    ordering: tuple[str, ...] | None
    inversions: int

    @property
    def is_stable(self) -> bool:
        """True when one ordering holds across the entire swept range."""
        return self.inversions == 0 and self.ordering is not None

    def crossover_points(self) -> list[float]:
        """Hazard values where the ordering changes. Empty when stable."""
        if len(self.arms) != 2:
            return []
        left, right = self.arms
        crossings = []
        deltas = [
            self._counts[left][i] - self._counts[right][i]
            for i in range(len(self.hazard_points))
        ]
        for i in range(1, len(deltas)):
            if deltas[i - 1] == 0 or deltas[i] == 0:
                continue
            if (deltas[i - 1] > 0) != (deltas[i] > 0):
                crossings.append(self.hazard_points[i])
        return crossings

    def describe(self) -> str:
        if self.ordering is None:
            return "no consistent ordering across the swept hazard range"
        chain = " > ".join(self.ordering)
        if self.is_stable:
            low, high = self.hazard_points[0], self.hazard_points[-1]
            return (
                f"{chain} preserves more mandates at every hazard in "
                f"[{low:.3f}, {high:.3f}] ({len(self.hazard_points)} points)"
            )
        return f"{chain} holds at most points, but the ordering inverts {self.inversions}x"


def mandate_survival_dominance(
    arms: Sequence[Arm],
    world: World,
    calendar: ComplianceCalendar,
    hazard_range: tuple[float, float],
    *,
    points: int = 9,
    costs: CostModel | None = None,
) -> DominanceResult:
    """Sweep the revocation hazard and report the ordering, never the counts.

    Endpoints are included deliberately: if the ordering holds at both extremes
    of a range chosen to be wide, it holds for any rate a reader might prefer
    inside it.

    This is C5's narrow version. C8 generalises the sweep to every parameter and
    every metric; the discipline is the same.
    """
    low, high = hazard_range
    step = (high - low) / (points - 1) if points > 1 else 0.0
    hazard_points = tuple(low + step * index for index in range(points))

    counts: dict[str, list[int]] = {arm.name: [] for arm in arms}
    orderings: list[tuple[str, ...]] = []

    for hazard in hazard_points:
        variant = world.with_mandate_hazard(hazard)
        result = run_comparison(arms, variant, calendar, costs=costs)
        for name, metrics in result.metrics.items():
            counts[name].append(metrics.mandates_preserved)
        orderings.append(
            tuple(
                sorted(
                    result.metrics,
                    key=lambda name: -result.metrics[name].mandates_preserved,
                )
            )
        )

    first = orderings[0]
    inversions = sum(1 for ordering in orderings if ordering != first)
    return DominanceResult(
        arms=tuple(arm.name for arm in arms),
        hazard_points=hazard_points,
        _counts={name: tuple(values) for name, values in counts.items()},
        ordering=first if inversions == 0 else first,
        inversions=inversions,
    )


# ------------------------------------------------- halted vs revoked -------


@dataclass(frozen=True)
class ExchangeRate:
    """Halts added per revocation avoided, at one hazard.

    Halted and revoked are two exit doors with different costs, not two
    failures. Halted preserves mandate authority -- a card update reactivates
    the subscription with no customer re-authorisation. Revoked destroys it:
    recovery needs full re-registration, a fresh PDN, fresh AFA, and the
    customer opening their UPI app, against Razorpay's own ~30% pre-registration
    drop-off.

    So trading revocations for halts is a real gain. This is the price of that
    trade, reported rather than asserted, because whether it is a *good* trade
    depends on how often halted subscriptions are actually recovered manually --
    a rate nobody publishes.
    """

    challenger: str
    incumbent: str
    hazard: float
    revocations_avoided: int
    halts_added: int

    @property
    def rate(self) -> float | None:
        if self.revocations_avoided <= 0:
            return None
        return self.halts_added / self.revocations_avoided


@dataclass(frozen=True)
class ExchangeRateBand:
    challenger: str
    incumbent: str
    rates: tuple[ExchangeRate, ...]

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(r.rate for r in self.rates if r.rate is not None)

    def describe(self) -> str:
        values = self.values
        if not values:
            return (
                f"{self.challenger} avoids no revocations against {self.incumbent} "
                "at any hazard in the swept range"
            )
        span = (
            f"{min(values):.1f}-{max(values):.1f}"
            if min(values) != max(values)
            else f"{min(values):.1f}"
        )
        return (
            f"{self.challenger} vs {self.incumbent}: {span} additional halts per "
            "revocation avoided, across the swept hazard range"
        )


def exchange_rate_band(
    arms: Sequence[Arm],
    world: World,
    calendar: ComplianceCalendar,
    hazard_range: tuple[float, float],
    challenger: str,
    incumbent: str,
    *,
    points: int = 5,
    costs: CostModel | None = None,
) -> ExchangeRateBand:
    """Sweep the hazard and report the halt/revocation exchange rate as a band."""
    low, high = hazard_range
    step = (high - low) / (points - 1) if points > 1 else 0.0
    rates = []
    for index in range(points):
        hazard = low + step * index
        result = run_comparison(
            arms, world.with_mandate_hazard(hazard), calendar, costs=costs
        )
        challenger_metrics = result.metrics[challenger]
        incumbent_metrics = result.metrics[incumbent]
        rates.append(
            ExchangeRate(
                challenger=challenger,
                incumbent=incumbent,
                hazard=hazard,
                revocations_avoided=(
                    incumbent_metrics.mandates_revoked - challenger_metrics.mandates_revoked
                ),
                halts_added=(
                    challenger_metrics.mandates_halted - incumbent_metrics.mandates_halted
                ),
            )
        )
    return ExchangeRateBand(challenger=challenger, incumbent=incumbent, rates=tuple(rates))
