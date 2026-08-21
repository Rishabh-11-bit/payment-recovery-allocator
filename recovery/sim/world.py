"""World sampling.

A *world* is one draw from the ranges in `config/worlds.yaml`. Every cardinal
value in the simulator comes from here, and every one of them is sampled rather
than fixed -- C8's robustness sweep is many worlds, so parameterisation has to
be structural from the first commit rather than retrofitted onto constants.

Nothing in the policy path may read a `World`. The arms receive observations;
the environment holds the world.
"""

from __future__ import annotations

import pathlib
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import yaml

from recovery.models import FailureClass

DEFAULT_WORLDS_PATH = pathlib.Path("config/worlds.yaml")

Range = tuple[float, float]


class WorldConfigError(ValueError):
    pass


def _as_range(raw: Any, where: str) -> Range:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 2:
        raise WorldConfigError(f"{where}: expected a [low, high] range, got {raw!r}")
    low, high = float(raw[0]), float(raw[1])
    if low > high:
        raise WorldConfigError(f"{where}: range [{low}, {high}] is inverted")
    return (low, high)


def _sample(rng: random.Random, span: Range) -> float:
    low, high = span
    return low if low == high else rng.uniform(low, high)


def _sample_mix(
    rng: random.Random, raw: Mapping[str, Any], where: str
) -> dict[str, float]:
    """Sample a set of proportions, then normalise.

    Ranges are specified independently and will not sum to 1. Normalising after
    sampling keeps each dimension's range meaningful while guaranteeing a valid
    distribution; the alternative -- rejection sampling until they sum -- would
    silently narrow the ranges being swept.
    """
    drawn = {key: _sample(rng, _as_range(value, f"{where}.{key}")) for key, value in raw.items()}
    total = sum(drawn.values())
    if total <= 0:
        raise WorldConfigError(f"{where}: proportions sum to {total}")
    return {key: value / total for key, value in drawn.items()}


@dataclass(frozen=True)
class RecoveryCurve:
    """P(retry succeeds) as a function of days since the original failure."""

    base: float
    per_day: float
    cap: float

    def probability(self, days_since_failure: int) -> float:
        if days_since_failure < 1:
            return 0.0
        raw = self.base + self.per_day * (days_since_failure - 1)
        return max(0.0, min(self.cap, raw))


@dataclass(frozen=True)
class World:
    """One sampled world. Immutable, and never visible to an arm."""

    seed: int
    horizon_days: int
    batch_size: int
    class_mix: Mapping[str, float]
    rail_mix: Mapping[str, float]
    amount_paise: Range
    recovery: Mapping[FailureClass, RecoveryCurve]
    link_conversion: Mapping[FailureClass, float]
    revocation_per_notification: float
    fatigue_multiplier: float
    revocation_class_multiplier: Mapping[FailureClass, float]
    emission_fidelity: float
    remaining_lifetime_months: float

    def revocation_hazard(self, failure_class: FailureClass, notification_index: int) -> float:
        """P(revoke) for the nth customer-visible notification, n from 1.

        Ordinal content: more notifications means more hazard, and the increase
        compounds. The magnitudes are swept, and no result may be quoted at a
        single point in that range.
        """
        base = self.revocation_per_notification
        base *= self.revocation_class_multiplier[failure_class]
        base *= self.fatigue_multiplier ** max(0, notification_index - 1)
        return min(1.0, base)


def load_world_config(path: pathlib.Path | str = DEFAULT_WORLDS_PATH) -> dict[str, Any]:
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"world config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise WorldConfigError(f"{path} is not a mapping")
    return raw


def sample_world(
    seed: int,
    raw: Mapping[str, Any] | None = None,
    *,
    path: pathlib.Path | str = DEFAULT_WORLDS_PATH,
) -> World:
    """Draw one world. Same seed and same config always give the same world."""
    raw = dict(raw if raw is not None else load_world_config(path))
    rng = random.Random(seed)

    batch = raw.get("batch") or {}
    recovery_raw = raw.get("recovery") or {}
    link_raw = raw.get("link_conversion") or {}
    mandate = raw.get("mandate") or {}

    missing = [c.value for c in FailureClass if c.value not in recovery_raw]
    if missing:
        raise WorldConfigError(f"recovery: missing curve(s) for {missing}")

    recovery: dict[FailureClass, RecoveryCurve] = {}
    for failure_class in FailureClass:
        spec = recovery_raw[failure_class.value]
        where = f"recovery.{failure_class.value}"
        recovery[failure_class] = RecoveryCurve(
            base=_sample(rng, _as_range(spec["base"], f"{where}.base")),
            per_day=_sample(rng, _as_range(spec["per_day"], f"{where}.per_day")),
            cap=_sample(rng, _as_range(spec["cap"], f"{where}.cap")),
        )

    if recovery[FailureClass.TERMINAL].cap != 0.0:
        raise WorldConfigError(
            "recovery.TERMINAL must be identically zero: P(retry succeeds | expired "
            "card or cancelled mandate) = 0 is definitional, not a sampled parameter"
        )

    return World(
        seed=seed,
        horizon_days=int(raw.get("horizon_days", 10)),
        batch_size=int(batch.get("size", 500)),
        class_mix=_sample_mix(rng, batch.get("class_mix") or {}, "batch.class_mix"),
        rail_mix=_sample_mix(rng, batch.get("rail_mix") or {}, "batch.rail_mix"),
        amount_paise=_as_range(batch.get("amount_paise"), "batch.amount_paise"),
        recovery=recovery,
        link_conversion={
            failure_class: _sample(
                rng,
                _as_range(
                    link_raw[failure_class.value], f"link_conversion.{failure_class.value}"
                ),
            )
            for failure_class in FailureClass
        },
        revocation_per_notification=_sample(
            rng,
            _as_range(
                mandate.get("revocation_per_notification"),
                "mandate.revocation_per_notification",
            ),
        ),
        fatigue_multiplier=_sample(
            rng, _as_range(mandate.get("fatigue_multiplier"), "mandate.fatigue_multiplier")
        ),
        revocation_class_multiplier={
            failure_class: _sample(
                rng,
                _as_range(
                    (mandate.get("class_multiplier") or {})[failure_class.value],
                    f"mandate.class_multiplier.{failure_class.value}",
                ),
            )
            for failure_class in FailureClass
        },
        emission_fidelity=_sample(
            rng, _as_range((raw.get("emission") or {}).get("fidelity"), "emission.fidelity")
        ),
        remaining_lifetime_months=_sample(
            rng,
            _as_range(
                (raw.get("ltv") or {}).get("remaining_lifetime_months"),
                "ltv.remaining_lifetime_months",
            ),
        ),
    )
