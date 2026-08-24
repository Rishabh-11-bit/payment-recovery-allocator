"""C12 -- holdout harness. The instrument that would measure magnitude.

## Why this exists, which is the whole point

Every rupee figure in this repo is simulated. The simulator's recovery dynamics
were written by hand, so an uplift measured against them measures nothing except
the ability to invert a function we wrote (CHALLENGES 002). The project's answer
was to split the claim: correctness proved by property tests, robustness by a
sweep, and **magnitude not claimed at all**.

This is what would claim it. A routing flag sends a stratified fraction of
eligible cases to the documented baseline on real traffic, and uplift is computed
from realised outcomes rather than from assumed ones.

**Its value is the claim, not the code.** It is short on purpose. "We cannot
measure magnitude on synthetic data, and here is the instrument that would
measure it on real volume" is a far stronger position than a synthetic number
with a disclaimer -- and it is only credible if the instrument exists.

## Assignment is deterministic, and that matters

A case's arm comes from a hash of its chain key and the experiment name. No
stored state, no random draw:

* The same case gets the same arm on a retry, so a replayed webhook cannot move
  a case between arms mid-experiment and contaminate both.
* Assignment survives a process restart with nothing persisted.
* Changing the experiment name reshuffles everybody, so a second experiment is
  not silently correlated with the first.

## Stratification

Failure class and rail both drive recovery, and both are unevenly distributed.
An unstratified 10% holdout can easily draw a TERMINAL-heavy control and make
the treatment look good for a reason that has nothing to do with the policy.
Assignment therefore hashes within a stratum, so each stratum is split at the
target fraction independently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

TREATMENT = "C"
CONTROL = "A"


def _bucket(*parts: str) -> float:
    """Stable [0, 1) from the parts. SHA-256 so it does not vary by process."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


@dataclass(frozen=True)
class Assignment:
    chain_key: str
    stratum: str
    arm: str
    bucket: float

    @property
    def is_control(self) -> bool:
        return self.arm == CONTROL


def stratum_of(rail: str, failure_class: str) -> str:
    return f"{rail}:{failure_class}"


def assign(
    chain_key: str,
    *,
    rail: str,
    failure_class: str,
    experiment: str,
    control_fraction: float,
) -> Assignment:
    """Which arm this case runs. Deterministic, stateless, stratified."""
    if not 0.0 <= control_fraction <= 1.0:
        raise ValueError(f"control_fraction {control_fraction} outside [0, 1]")
    stratum = stratum_of(rail, failure_class)
    bucket = _bucket(experiment, stratum, chain_key)
    return Assignment(
        chain_key=chain_key,
        stratum=stratum,
        arm=CONTROL if bucket < control_fraction else TREATMENT,
        bucket=bucket,
    )


@dataclass
class Outcome:
    """One realised case. Recorded after the fact, never predicted."""

    chain_key: str
    arm: str
    stratum: str
    recovered_paise: int
    executions_spent: int
    contacts_sent: int
    mandate_revoked: bool


@dataclass
class StratumResult:
    stratum: str
    control_cases: int = 0
    treatment_cases: int = 0
    control_recovered: int = 0
    treatment_recovered: int = 0

    @property
    def control_mean(self) -> float:
        return self.control_recovered / self.control_cases if self.control_cases else 0.0

    @property
    def treatment_mean(self) -> float:
        return (
            self.treatment_recovered / self.treatment_cases if self.treatment_cases else 0.0
        )

    @property
    def uplift_per_case_paise(self) -> float:
        return self.treatment_mean - self.control_mean


@dataclass
class HoldoutResult:
    experiment: str
    strata: Mapping[str, StratumResult]
    control_cases: int = 0
    treatment_cases: int = 0
    control_recovered_paise: int = 0
    treatment_recovered_paise: int = 0
    control_revoked: int = 0
    treatment_revoked: int = 0

    @property
    def uplift_per_case_paise(self) -> float:
        """Stratum-weighted, not a raw pooled difference.

        Pooling would let an imbalance in stratum sizes masquerade as an effect,
        which is the failure stratification exists to prevent -- so undoing it at
        the reporting step would waste the design.
        """
        total = sum(s.control_cases + s.treatment_cases for s in self.strata.values())
        if not total:
            return 0.0
        return sum(
            s.uplift_per_case_paise * (s.control_cases + s.treatment_cases) / total
            for s in self.strata.values()
        )

    @property
    def underpowered_strata(self) -> tuple[str, ...]:
        """Strata with an empty side. Their uplift is arithmetic, not evidence."""
        return tuple(
            name
            for name, s in sorted(self.strata.items())
            if not s.control_cases or not s.treatment_cases
        )

    def describe(self) -> str:
        lines = [
            f"holdout '{self.experiment}': "
            f"{self.control_cases} control / {self.treatment_cases} treatment"
        ]
        lines.append(
            f"  uplift Rs {self.uplift_per_case_paise / 100:,.2f} per case "
            "(stratum-weighted, realised outcomes)"
        )
        lines.append(
            f"  mandates revoked: control {self.control_revoked}, "
            f"treatment {self.treatment_revoked}"
        )
        if self.underpowered_strata:
            lines.append(
                f"  {len(self.underpowered_strata)} stratum/strata have an empty side "
                "and contribute arithmetic rather than evidence: "
                + ", ".join(self.underpowered_strata)
            )
        lines.append(
            "  NOT a significance test. This reports a realised difference and the "
            "volume behind it;"
        )
        lines.append(
            "  whether that difference is distinguishable from noise needs a power "
            "calculation this does not do."
        )
        return "\n".join(lines)


def measure(experiment: str, outcomes: Iterable[Outcome]) -> HoldoutResult:
    """Uplift from realised outcomes. Nothing here models or predicts."""
    strata: dict[str, StratumResult] = {}
    result = HoldoutResult(experiment=experiment, strata=strata)

    for outcome in outcomes:
        stratum = strata.setdefault(outcome.stratum, StratumResult(stratum=outcome.stratum))
        if outcome.arm == CONTROL:
            result.control_cases += 1
            result.control_recovered_paise += outcome.recovered_paise
            result.control_revoked += int(outcome.mandate_revoked)
            stratum.control_cases += 1
            stratum.control_recovered += outcome.recovered_paise
        else:
            result.treatment_cases += 1
            result.treatment_recovered_paise += outcome.recovered_paise
            result.treatment_revoked += int(outcome.mandate_revoked)
            stratum.treatment_cases += 1
            stratum.treatment_recovered += outcome.recovered_paise

    return result
