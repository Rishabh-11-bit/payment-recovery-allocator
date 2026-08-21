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
