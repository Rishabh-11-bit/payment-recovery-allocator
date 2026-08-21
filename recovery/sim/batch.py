"""Synthetic failure batch.

Each failure carries a **hidden true class** and an **observed payload**. The
arms see only the payload; the environment resolves outcomes against the truth.

The emission model is the point of this module. If the true class were readable
straight off the payload, classification would be trivial, the cost matrix would
never bind, and the comparison would be measuring nothing. So emission is noisy:
at fidelity `f` the payload carries the true class's characteristic
`(source, step, reason)`, and otherwise it carries another class's -- the same
ambiguity a real error taxonomy has, where `payment_debit_response` means
LIQUIDITY or INFRASTRUCTURE depending on `source`.

Sweeping fidelity in C8 is how the result is shown not to depend on a clean
signal.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any, Mapping

from recovery.models import FailureClass
from recovery.sim.calendar import IST
from recovery.sim.world import World

# Characteristic payloads per (rail, class), drawn from the documented field
# reference. Method-partitioned: sources differ per method, and no `razorpay`
# source exists anywhere -- see CLAUDE.md's classifier section.
EMISSIONS: Mapping[str, Mapping[FailureClass, tuple[str, str, str]]] = {
    "upi": {
        FailureClass.INFRASTRUCTURE: ("gateway", "payment_initiation", "gateway_technical_error"),
        FailureClass.LIQUIDITY: ("customer_psp", "payment_debit_response", "insufficient_funds"),
        FailureClass.ATTENTION: ("customer", "payment_authentication", "payment_ux_canceled"),
        FailureClass.TERMINAL: ("beneficiary_bank", "payment_debit_response", "mandate_revoked"),
    },
    "card": {
        FailureClass.INFRASTRUCTURE: ("gateway", "payment_initiation", "gateway_technical_error"),
        FailureClass.LIQUIDITY: ("issuer_bank", "payment_authorization", "insufficient_funds"),
        FailureClass.ATTENTION: ("customer", "payment_authentication", "payment_ux_canceled"),
        FailureClass.TERMINAL: ("issuer_bank", "payment_authorization", "payment_expired_card"),
    },
    "emandate": {
        FailureClass.INFRASTRUCTURE: ("gateway", "payment_initiation", "gateway_technical_error"),
        FailureClass.LIQUIDITY: ("bank", "payment_debit_response", "insufficient_funds"),
        FailureClass.ATTENTION: ("customer", "payment_authentication", "payment_ux_canceled"),
        FailureClass.TERMINAL: ("bank", "payment_debit_response", "mandate_revoked"),
    },
}


@dataclass
class SyntheticFailure:
    """One failed mandate debit.

    `true_class` is ground truth and must never reach an arm. Everything an arm
    is allowed to see is in `observed()`.
    """

    case_id: str
    payment_id: str
    order_id: str
    rail: str
    amount_paise: int
    failed_at: dt.datetime
    true_class: FailureClass = field(repr=False)
    observed_source: str
    observed_step: str
    observed_reason: str
    emission_faithful: bool = field(repr=False, default=True)

    def observed(self) -> dict[str, Any]:
        """The payment entity as an arm sees it. No ground truth in here."""
        return {
            "id": self.payment_id,
            "order_id": self.order_id,
            "amount": self.amount_paise,
            "currency": "INR",
            "status": "failed",
            "method": self.rail,
            "error_source": self.observed_source,
            "error_step": self.observed_step,
            "error_reason": self.observed_reason,
        }


def _weighted_choice(rng: random.Random, weights: Mapping[str, float]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def generate_batch(
    world: World,
    *,
    start: dt.datetime | None = None,
    seed: int | None = None,
) -> list[SyntheticFailure]:
    """Deterministic given `world.seed` (or an explicit `seed`)."""
    rng = random.Random(seed if seed is not None else world.seed * 7919 + 13)
    start = start or dt.datetime(2026, 3, 2, 3, 0, tzinfo=IST)

    failures: list[SyntheticFailure] = []
    for index in range(world.batch_size):
        rail = _weighted_choice(rng, world.rail_mix)
        true_class = FailureClass(_weighted_choice(rng, world.class_mix))

        faithful = rng.random() < world.emission_fidelity
        if faithful:
            emitted_class = true_class
        else:
            # Confusable with something else -- which is what makes the cost
            # matrix matter rather than decorate.
            others = [c for c in FailureClass if c is not true_class]
            emitted_class = rng.choice(others)
        source, step, reason = EMISSIONS[rail][emitted_class]

        failures.append(
            SyntheticFailure(
                case_id=f"case_{index:05d}",
                payment_id=f"pay_SIM{index:08d}",
                order_id=f"order_SIM{index:08d}",
                rail=rail,
                amount_paise=int(rng.uniform(*world.amount_paise)),
                # Original debit lands in a non-peak morning slot.
                failed_at=start + dt.timedelta(minutes=rng.randrange(0, 120)),
                true_class=true_class,
                observed_source=source,
                observed_step=step,
                observed_reason=reason,
                emission_faithful=faithful,
            )
        )
    return failures
