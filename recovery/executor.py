"""C10-execution -- dispatching a shaped checkout as a real Razorpay Payment Link.

`recovery/rail_actions.py` builds the `options.checkout` payload and stops:
"it builds payloads. It does not decide... and it does not execute." Nothing
downstream of it ever called Razorpay's API. `CLAUDE.md` claimed an adapter
pattern here -- `SimulatorExecutor` primary, `RazorpayExecutor` demonstrated
only -- and neither class existed. That was a documentation claim with no
code behind it, caught while building this module. See CHALLENGES.md.

## Scope: contact actions only, and that boundary is not new here

`RECOVERY_LINK`, `OFFER_RAIL_MIGRATION`, `REORDER_RAILS` and
`EXCLUDE_INSTRUMENT` all resolve to one Razorpay primitive: a Payment Link the
customer completes themselves. That primitive is real, documented, and
callable today.

`SCHEDULE_AT` is not given a live counterpart, and that is not a limitation
introduced here -- it is `CLAUDE.md`'s standing rule restated at the
execution layer: **`ATTEMPT_NOW` does not exist**, because there is no
API that lets a third party force a mandate execution. A scheduled execution
is a compliance-checked *time*, honoured by NPCI's own rails once the mandate
reaches it, not a call this system makes. Building a live path for it would
mean inventing an endpoint Razorpay does not expose.

## Adapter pattern, matching gateway.py exactly

Same shape as `recovery.gateway`: a `Protocol`, an in-memory
`SimulatedExecutor` that is what the simulator, the worker's default, and
every test run against, and a `RazorpayExecutor` that is real, stdlib-only
(no SDK dependency added), and refuses at construction to run against
anything that is not a `rzp_test_` key -- the identical guard
`RazorpayGateway` already carries, copied rather than reinvented so the two
adapters fail the same way for the same reason.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from recovery.rail_actions import CheckoutShaping


class ExecutionError(RuntimeError):
    """The dispatch failed. Never silently treated as a skip."""


@dataclasses.dataclass(frozen=True)
class ExecutionResult:
    """What happened when a shaped checkout was turned into an offer.

    `source` is `simulated` or `razorpay`, printed everywhere this result is
    logged -- a real link and a simulated one must never look alike in the
    audit trail.
    """

    reference_id: str
    source: str  # "simulated" | "razorpay"
    link_id: str | None
    short_url: str | None
    status: str
    detail: str = ""


class Executor(Protocol):
    def create_recovery_link(
        self,
        *,
        reference_id: str,
        amount_paise: int,
        description: str,
        shaping: CheckoutShaping,
    ) -> ExecutionResult: ...


class SimulatedExecutor:
    """In-memory. Records what would have been created; calls nothing.

    This is the default everywhere -- the worker, `reproduce`, every test.
    Nothing in this project's reported figures depends on a network call ever
    having happened, and this class is why: swapping it for `RazorpayExecutor`
    changes what gets dispatched, never what gets decided or measured.
    """

    def __init__(self) -> None:
        self.created: list[ExecutionResult] = []

    def create_recovery_link(
        self,
        *,
        reference_id: str,
        amount_paise: int,
        description: str,
        shaping: CheckoutShaping,
    ) -> ExecutionResult:
        result = ExecutionResult(
            reference_id=reference_id,
            source="simulated",
            link_id=f"plink_SIMULATED_{len(self.created):06d}",
            short_url=None,
            status="created",
            detail=shaping.rationale,
        )
        self.created.append(result)
        return result


class RazorpayExecutor:
    """Real. Stdlib `urllib` only -- no SDK dependency added.

    `POST /v1/payment_links`, using the same request pattern and the same
    test-key guard as `RazorpayGateway`: credentials come from the caller, are
    never logged, and construction itself raises if the key is not a
    `rzp_test_` key. There is no code path in this project that can construct
    this class against a live key.
    """

    BASE = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, timeout: float = 15.0) -> None:
        if not key_id.startswith("rzp_test_"):
            raise ValueError("refusing to construct an executor with a non-test key")
        self._token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self._timeout = timeout

    def create_recovery_link(
        self,
        *,
        reference_id: str,
        amount_paise: int,
        description: str,
        shaping: CheckoutShaping,
    ) -> ExecutionResult:
        body: dict[str, Any] = {
            "amount": amount_paise,
            "currency": "INR",
            "description": description,
            "reference_id": reference_id,
            "options": dict(shaping.as_payment_link_payload(reference_id)["options"]),
            # Do not notify a real customer from a demonstration run. Creation
            # against Razorpay's test mode is exercised for real; delivery
            # never is -- that stays out of scope for the same DLT reason
            # NOT_BUILT.md gives for real SMS/WhatsApp.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        request = urllib.request.Request(
            f"{self.BASE}/payment_links",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Basic {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                entity = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Deliberately excludes the response body from the exception message:
            # a 401 body can echo back enough of the request to be worth not
            # logging verbatim. The status code alone is enough to diagnose.
            raise ExecutionError(
                f"payment link creation failed for {reference_id}: HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ExecutionError(
                f"payment link creation failed for {reference_id}: {type(exc).__name__}"
            ) from exc

        return ExecutionResult(
            reference_id=reference_id,
            source="razorpay",
            link_id=entity.get("id"),
            short_url=entity.get("short_url"),
            status=entity.get("status", "unknown"),
            detail=shaping.rationale,
        )
