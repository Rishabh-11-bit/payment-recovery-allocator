"""World sampling.

A *world* is one draw from the ranges in `config/worlds.yaml`. Every cardinal
value in the simulator comes from here, and every one of them is sampled rather
than fixed -- C8's robustness sweep is many worlds, so parameterisation has to
be structural from the first commit rather than retrofitted onto constants.

Nothing in the policy path may read a `World`. The arms receive observations;
the environment holds the world.
"""

from __future__ import annotations

import dataclasses
import pathlib
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import yaml

from recovery.calibration import load_profile
from recovery.models import FailureClass

DEFAULT_WORLDS_PATH = pathlib.Path("config/worlds.yaml")

Range = tuple[float, float]

# Card and Emandate revocation as a share of the UPI hazard. Near zero because
# there is no in-app cancel gesture on those rails. UNSOURCED -- the ordinal
# claim (far lower than UPI) is the part the argument uses. See ASSUMPTIONS.md.
NON_UPI_HAZARD_SHARE = 0.05


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
    # Which calibration profile the failure mix came from, so any reported
    # result can be traced to its provenance -- or to the absence of one.
    calibration_profile: str
    horizon_days: int
    batch_size: int
    class_mix: Mapping[str, float]
    rail_mix: Mapping[str, float]
    amount_paise: Range
    recovery: Mapping[FailureClass, RecoveryCurve]
    link_conversion: Mapping[FailureClass, float]
    revocation_per_notification: float
    fatigue_multiplier: float
    emission_fidelity: float
    # Sourced issuer-outage distribution, carried through from the profile.
    # Consumed by C11; recorded now so the sourced numbers are in the repo
    # before the component that needs them exists.
    issuer_outage: Mapping[str, Any] = field(default_factory=dict)

    def with_mandate_hazard(
        self, revocation_per_notification: float, fatigue_multiplier: float | None = None
    ) -> World:
        """A copy of this world with the revocation hazard moved.

        Used to sweep the one parameter that mandate-survival counts depend on.
        The counts themselves are never reported; only whether the *ordering*
        between arms holds across the range.
        """
        return dataclasses.replace(
            self,
            revocation_per_notification=revocation_per_notification,
            fatigue_multiplier=(
                self.fatigue_multiplier if fatigue_multiplier is None else fatigue_multiplier
            ),
        )

    def revocation_hazard(
        self,
        failure_class: FailureClass,
        notification_index: int,
        rail: str | None = None,
    ) -> float:
        """P(revoke) for the nth customer-visible notification, n from 1.

        Ordinal content: more notifications means more hazard, and the increase
        compounds. The magnitudes are swept, and no result may be quoted at a
        single point in that range.

        Deliberately independent of failure class. A per-class multiplier was
        tested and removed: flattening it to 1.0 changed no conclusion, so the
        result never depended on it and it was an unsourced claim about customer
        psychology earning nothing.
        """
        del failure_class  # the hazard is class-independent; see ASSUMPTIONS.md

        # RAIL-CONDITIONAL. Revocation is a UPI phenomenon: the customer opens
        # their PSP app and cancels the mandate in two taps. There is no
        # equivalent gesture for a card mandate or an e-NACH -- cancelling those
        # means contacting the bank or the merchant, so the hazard is near zero
        # rather than merely lower.
        #
        # Applying a UPI-shaped hazard to every rail overstated the mandate
        # argument on exactly the rails where it does not hold.
        if rail is not None and rail != "upi":
            return min(1.0, self.revocation_per_notification * NON_UPI_HAZARD_SHARE)

        base = self.revocation_per_notification
        base *= self.fatigue_multiplier ** max(0, notification_index - 1)
        return min(1.0, base)


def _resolve_class_mix(batch: Mapping[str, Any]):
    """The profile to sample from, or None when an inline override is in force.

    An inline `class_mix` overrides the profile and is reported as
    `inline-override` rather than borrowing a profile's name -- a result taken
    from an override has no provenance and must not appear to have one.
    """
    if batch.get("class_mix"):
        return None, batch["class_mix"]
    name = batch.get("calibration_profile")
    if not name:
        raise WorldConfigError(
            "batch needs either `calibration_profile` or an inline `class_mix`"
        )
    return load_profile(str(name)), {}


def mandate_hazard_range(raw: Mapping[str, Any]) -> Range:
    """The configured revocation range, for sweeping rather than for sampling."""
    return _as_range(
        (raw.get("mandate") or {}).get("revocation_per_notification"),
        "mandate.revocation_per_notification",
    )


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
    profile, class_mix_ranges = _resolve_class_mix(batch)
    profile_name = profile.name if profile is not None else "inline-override"
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
        calibration_profile=profile_name,
        issuer_outage=profile.issuer_outage if profile is not None else {},
        horizon_days=int(raw.get("horizon_days", 10)),
        batch_size=int(batch.get("size", 500)),
        class_mix=(
            profile.sample_mix(rng)
            if profile is not None
            else _sample_mix(rng, class_mix_ranges, "batch.class_mix")
        ),
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
        emission_fidelity=_sample(
            rng, _as_range((raw.get("emission") or {}).get("fidelity"), "emission.fidelity")
        ),
    )
