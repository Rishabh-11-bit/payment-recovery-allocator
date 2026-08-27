"""C11 -- storm governor: jitter and a per-issuer admission ceiling.

## Regulatory basis, not a speculative feature

NPCI directs PSPs to initiate mandate executions at **moderated TPS**, and may
apply rate limiters to avoid spikes. A recovery system that schedules T+1 for
every failure in a batch produces exactly the spike the directive exists to
prevent -- and it aims that spike at whichever issuer just had an incident,
because that is why the batch failed.

So the governor does two things:

* **Jitter** spreads scheduled executions inside their compliant window instead
  of stacking them on one minute.
* **A per-issuer ceiling** caps how many executions may be admitted against one
  issuer in a window.

## Observed conditions, never a static list

This is the design consequence of CHALLENGES 011. Four months of NPCI per-bank
downtime show 25 distinct banks, 12 of them appearing in exactly one month --
outages rotate. Two banks are persistent, and the second was only found after
normalising spelling: Punjab National Bank looked fine in April (1.67h) and was
five times worse by June (9.40h).

**A static blocklist would have been built from April's data and would have
missed it.** The bank that needs throttling next month is not reliably one that
looks bad today. So the ceiling is a function of what an issuer is doing *now*,
computed from a rolling window, and an issuer that recovers is released
automatically. No name is ever hardcoded.

The sourced outage distribution in `bounded-2026` supplies the thresholds --
mean share per affected bank, and the upper end of the per-bank range -- so
"degraded" means "worse than what NPCI actually published", not a number
somebody picked.

## The Downtime webhook, and its blind spot

`payment.downtime.*` carries `severity`, `status` and `instrument_schema`, and
`status: scheduled` means a planned outage is known in advance.

`instrument_schema` is keyed differently per method -- UPI by `vpa_handle` or
`psp`, cards by `issuer` or `network`, netbanking by `bank`.

**The gap, stated because a clean feed is not the same as coverage:** a PSP is
flagged down only when *all* of its handles are down. A PSP degrading on one
handle -- the common shape -- produces no notice at all. The webhook is
high-precision and low-recall: trust it when it fires, never read silence as
health. That is why the observed-failure-rate path remains rather than being
replaced by it.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Mapping

# Fallbacks used only when no sourced distribution is supplied. Both come from
# the four NPCI months and are documented in config/calibration/bounded-2026.yaml.
DEFAULT_HEALTHY_SHARE = 0.0078  # upper end of the observed per-month mean
DEFAULT_DEGRADED_SHARE = 0.0265  # worst single bank-month observed


class IssuerState(str, enum.Enum):
    HEALTHY = "healthy"
    STRAINED = "strained"
    DEGRADED = "degraded"


# Instrument key by method, from the Downtime webhook's `instrument_schema`.
# The field differs per method -- keying everything on "issuer" would silently
# discard every UPI notice, which is the rail that matters most here.
INSTRUMENT_KEY = {
    "upi": ("vpa_handle", "psp"),
    "card": ("issuer", "network"),
    "netbanking": ("bank",),
    "emandate": ("bank",),
}


@dataclass(frozen=True)
class DowntimeNotice:
    """A `payment.downtime.*` webhook.

    Better evidence than our own failure rate on two counts: it comes from the
    network rather than being inferred, and `status: scheduled` arrives *before*
    the outage, so a planned window can be avoided rather than spent into. It is
    the only signal in this system that arrives ahead of the failure it predicts.
    """

    method: str
    instrument: str
    severity: str
    status: str
    at: dt.datetime

    @property
    def is_scheduled(self) -> bool:
        return self.status == "scheduled"

    @property
    def is_active(self) -> bool:
        return self.status in ("started", "scheduled")

    @classmethod
    def from_payload(cls, entity: Mapping, at: dt.datetime) -> "DowntimeNotice":
        method = str(entity.get("method", "")).lower()
        schema = entity.get("instrument_schema") or entity.get("instrument") or {}
        instrument = ""
        for field_name in INSTRUMENT_KEY.get(method, ()):
            if schema.get(field_name):
                instrument = str(schema[field_name]).lower()
                break
        return cls(
            method=method,
            instrument=instrument,
            severity=str(entity.get("severity", "")).lower(),
            status=str(entity.get("status", "")).lower(),
            at=at,
        )


@dataclass(frozen=True)
class Observation:
    """One executed attempt and how it went. The governor's only input."""

    issuer: str
    at: dt.datetime
    failed: bool


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    issuer_state: IssuerState
    reason: str = ""
    ceiling: int = 0
    admitted_in_window: int = 0

    def __str__(self) -> str:
        if self.admitted:
            return f"admitted ({self.issuer_state.value})"
        return f"held: {self.reason}"


@dataclass
class _IssuerWindow:
    observations: Deque[Observation] = field(default_factory=deque)
    admitted: Deque[dt.datetime] = field(default_factory=deque)

    def trim(self, cutoff: dt.datetime) -> None:
        while self.observations and self.observations[0].at < cutoff:
            self.observations.popleft()
        while self.admitted and self.admitted[0] < cutoff:
            self.admitted.popleft()


class StormGovernor:
    """Per-issuer admission control driven by observed conditions.

    Holds no list of bad issuers, and cannot: an issuer is known to it only by
    what has been observed of it inside the window, and that record ages out.
    """

    def __init__(
        self,
        *,
        window_hours: int = 24,
        base_ceiling: int = 50,
        strained_ceiling: int = 20,
        degraded_ceiling: int = 5,
        min_observations: int = 10,
        strained_failure_share: float = DEFAULT_HEALTHY_SHARE * 20,
        degraded_failure_share: float = DEFAULT_DEGRADED_SHARE * 20,
        jitter_minutes: int = 45,
    ) -> None:
        self.window = dt.timedelta(hours=window_hours)
        self.base_ceiling = base_ceiling
        self.strained_ceiling = strained_ceiling
        self.degraded_ceiling = degraded_ceiling
        self.min_observations = min_observations
        self.strained_failure_share = strained_failure_share
        self.degraded_failure_share = degraded_failure_share
        self.jitter_minutes = jitter_minutes
        self._issuers: dict[str, _IssuerWindow] = {}
        # Active downtime notices, keyed by instrument. Not a blocklist:
        # entries are added and removed by the network's own started/resolved
        # events, and nothing here is ever typed by hand.
        self._downtime: dict[str, DowntimeNotice] = {}

    # ------------------------------------------------------------ observe --

    def observe(self, issuer: str, at: dt.datetime, *, failed: bool) -> None:
        """Record an outcome. This is the only way an issuer becomes known."""
        window = self._issuers.setdefault(issuer, _IssuerWindow())
        window.observations.append(Observation(issuer=issuer, at=at, failed=failed))
        window.trim(at - self.window)

    def observe_downtime(self, notice: DowntimeNotice) -> None:
        """Consume a Downtime webhook.

        An active high-severity notice drives the instrument to DEGRADED at
        once, without waiting for `min_observations` failures -- waiting would
        mean spending exactly the executions the notice was warning about.
        """
        key = notice.instrument or notice.method
        if not key:
            return
        if notice.is_active:
            self._downtime[key] = notice
        else:
            self._downtime.pop(key, None)

    def downtime_state(self, issuer: str) -> IssuerState | None:
        notice = self._downtime.get(issuer)
        if notice is None:
            return None
        return IssuerState.DEGRADED if notice.severity == "high" else IssuerState.STRAINED

    def failure_share(self, issuer: str, now: dt.datetime) -> float | None:
        """Observed failure share in the window, or None below the threshold.

        None rather than zero: an issuer with three observations has not been
        measured, and treating "no evidence" as "healthy" is how a governor
        misses a bank that is on its way down.
        """
        window = self._issuers.get(issuer)
        if window is None:
            return None
        window.trim(now - self.window)
        if len(window.observations) < self.min_observations:
            return None
        failures = sum(1 for o in window.observations if o.failed)
        return failures / len(window.observations)

    def state_of(self, issuer: str, now: dt.datetime) -> IssuerState:
        # A live downtime notice outranks the observed rate: it is direct
        # evidence, and for a scheduled outage it is evidence held before any
        # failure has happened.
        declared = self.downtime_state(issuer)
        if declared is not None:
            return declared

        share = self.failure_share(issuer, now)
        if share is None:
            return IssuerState.HEALTHY
        if share >= self.degraded_failure_share:
            return IssuerState.DEGRADED
        if share >= self.strained_failure_share:
            return IssuerState.STRAINED
        return IssuerState.HEALTHY

    def ceiling_for(self, issuer: str, now: dt.datetime) -> int:
        return {
            IssuerState.HEALTHY: self.base_ceiling,
            IssuerState.STRAINED: self.strained_ceiling,
            IssuerState.DEGRADED: self.degraded_ceiling,
        }[self.state_of(issuer, now)]

    # -------------------------------------------------------------- admit --

    def admit(self, issuer: str, now: dt.datetime, *, record: bool = True) -> Verdict:
        """Ceiling check against what has already been admitted in the window."""
        window = self._issuers.setdefault(issuer, _IssuerWindow())
        window.trim(now - self.window)

        state = self.state_of(issuer, now)
        ceiling = self.ceiling_for(issuer, now)
        used = len(window.admitted)

        if used >= ceiling:
            return Verdict(
                admitted=False,
                issuer_state=state,
                reason=(
                    f"issuer admission ceiling reached: {used}/{ceiling} in the last "
                    f"{int(self.window.total_seconds() // 3600)}h "
                    f"({state.value})"
                ),
                ceiling=ceiling,
                admitted_in_window=used,
            )

        if record:
            window.admitted.append(now)
        return Verdict(
            admitted=True,
            issuer_state=state,
            ceiling=ceiling,
            admitted_in_window=used + 1,
        )

    # ------------------------------------------------------------- jitter --

    def jitter(self, execute_at: dt.datetime, key: str) -> dt.datetime:
        """Spread a scheduled execution deterministically.

        Derived from the key rather than drawn at random: the same case must get
        the same slot on a replay, or a retried worker reschedules the execution
        somewhere else and the audit trail stops reconstructing.

        Always forward. Moving an execution earlier could cross the PDN lead
        time or walk into a peak window, and the governor must never turn a
        compliant proposal into a non-compliant one.
        """
        digest = hashlib.sha256(key.encode()).digest()
        offset = int.from_bytes(digest[:4], "big") % (self.jitter_minutes + 1)
        return execute_at + dt.timedelta(minutes=offset)

    # ------------------------------------------------------------ inspect --

    def known_issuers(self) -> list[str]:
        return sorted(self._issuers)

    def snapshot(self, now: dt.datetime) -> dict[str, dict[str, object]]:
        """Current observed conditions. What a static list could never be."""
        return {
            issuer: {
                "state": self.state_of(issuer, now).value,
                "failure_share": self.failure_share(issuer, now),
                "ceiling": self.ceiling_for(issuer, now),
                "admitted_in_window": len(window.admitted),
                "observations": len(window.observations),
            }
            for issuer, window in sorted(self._issuers.items())
        }


def governor_from_config(config, world=None) -> StormGovernor:
    """Build from the `governor:` block, with thresholds from sourced outage.

    When a world carrying `issuer_outage` is supplied, the strained and degraded
    thresholds are scaled from the NPCI-published distribution rather than from
    numbers somebody picked -- "degraded" means "worse than what was actually
    observed", which is a defensible sentence in a review.
    """
    block = config.governor
    strained = block.strained_failure_share
    degraded = block.degraded_failure_share

    outage = getattr(world, "issuer_outage", None) if world is not None else None
    if outage:
        mean_high = float(outage["mean_share_per_affected_bank"][1])
        worst = float(outage["per_bank_share"][1])
        strained = min(1.0, mean_high * block.sourced_multiplier)
        degraded = min(1.0, worst * block.sourced_multiplier)

    return StormGovernor(
        window_hours=block.window_hours,
        base_ceiling=block.base_ceiling,
        strained_ceiling=block.strained_ceiling,
        degraded_ceiling=block.degraded_ceiling,
        min_observations=block.min_observations,
        strained_failure_share=strained,
        degraded_failure_share=degraded,
        jitter_minutes=block.jitter_minutes,
    )
