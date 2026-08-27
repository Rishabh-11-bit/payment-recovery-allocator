"""The three arms.

Arms propose; the environment disposes. No arm enforces its own compliance and
no arm can see ground truth -- both are structural, so a result cannot come from
an arm quietly breaking a rule or reading the answer.

Attribution is the reason there are three rather than two:

    A -> B   the value of *contacting people*
    B -> C   the value of *cause-awareness*

A -> C on its own conflates the two, which is the ambiguity Arm B exists to
remove.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from recovery.sim.calendar import IST, ComplianceCalendar
from recovery.sim.environment import ActionKind, CaseOutcome, CaseView, Proposal

# Razorpay Subscriptions documentation, "Failed Payments and Retries"
# (razorpay.com/docs/payments/subscriptions/). Reimplemented from the documented
# behaviour -- this baseline is not a strawman we invented to beat.
BASELINE_DOCS = "razorpay.com/docs/payments/subscriptions/ -- Failed Payments and Retries"


class Arm(Protocol):
    name: str

    def propose(self, view: CaseView, now: dt.datetime) -> list[Proposal]: ...


def _schedule(
    calendar: ComplianceCalendar,
    now: dt.datetime,
    target_day: dt.date,
    rail: str | None = None,
) -> dt.datetime | None:
    """First compliant moment on `target_day` that also clears the PDN lead time.

    Returns None when the day cannot host a compliant attempt, which the caller
    treats as "not today" rather than as an error.
    """
    earliest = max(
        dt.datetime.combine(target_day, dt.time(0, 30), tzinfo=IST),
        now + dt.timedelta(hours=calendar.pdn_lead_for(rail), minutes=10),
    )
    slot = calendar.next_compliant_slot(earliest)
    while slot is not None:
        violation = calendar.check_attempt(
            decided_at=now, execute_at=slot, attempts_used=0, rail=rail
        )
        if violation is None:
            return slot
        if "completion_deadline" in str(violation):
            slot = calendar.next_compliant_slot(
                dt.datetime.combine(
                    slot.astimezone(IST).date() + dt.timedelta(days=1),
                    dt.time(0, 30),
                    tzinfo=IST,
                )
            )
            continue
        return None
    return None


class ArmA:
    """Documented baseline. Rail-parameterised, cause-blind.

    Reimplemented from Razorpay's subscriptions documentation:

    * **Card and UPI** -- T+1, T+2, T+3 daily, then `halted`.
    * **Emandate** -- asynchronous. A retry is attempted only once confirmation
      or rejection of the previous payment arrives, which can exceed 24h. Charge
      day shifts around bank holidays: T -> T-1, or T -> T-3 when both T and
      T-1 are holidays.

    **The retry model never references the failure reason.** An expired card and
    an insufficient-balance decline get identical treatment, and so do a
    cancelled mandate and a gateway timeout. That is the source of the primary
    claim, and it is documented behaviour rather than a weakness we invented.
    """

    name = "A"
    docs = BASELINE_DOCS

    def __init__(self, calendar: ComplianceCalendar) -> None:
        self.calendar = calendar

    def propose(self, view: CaseView, now: dt.datetime) -> list[Proposal]:
        if view.outcome is not CaseOutcome.OPEN:
            return []
        if view.attempts_used >= self.calendar.attempt_cap:
            return []
        # Emandate: nothing until the prior attempt resolves.
        if view.attempt_pending:
            return []

        # attempts_used starts at 1 (the original execution), so this is the
        # T+n offset directly.
        offset = view.attempts_used

        if view.rail == "emandate":
            floor = view.last_attempt_resolved_at or view.failed_at
            target_day = max(floor.astimezone(IST).date(), now.astimezone(IST).date())
            target_day = target_day + dt.timedelta(days=1)
            target_day = self.calendar.shift_for_bank_holidays(target_day)
        else:
            target_day = (view.failed_at.astimezone(IST) + dt.timedelta(days=offset)).date()

        slot = _schedule(self.calendar, now, target_day, view.rail)
        if slot is None:
            return []
        return [
            Proposal(
                case_id=view.case_id,
                kind=ActionKind.ATTEMPT,
                execute_at=slot,
                note=f"baseline T+{offset} ({view.rail}); cause-blind",
            )
        ]


class ArmB:
    """Generic recovery: the baseline, plus one recovery link to every failure.

    No cause awareness, no instrument shaping, no budget reasoning. Every
    failure gets exactly one link regardless of whether a link could possibly
    help -- a dead card gets one, a cancelled mandate gets one.

    **Composition note (confirm this).** B is implemented as A + one contact, so
    that B - A isolates the value of contact and C - B isolates the value of
    cause-awareness. The alternative reading -- B as contact-only, with no
    retries -- would make B - A a mixture of "added contact" and "removed
    retries" and would not isolate anything. Switch with `include_baseline`.
    """

    name = "B"

    def __init__(self, calendar: ComplianceCalendar, *, include_baseline: bool = True) -> None:
        self.calendar = calendar
        self.include_baseline = include_baseline
        self._baseline = ArmA(calendar)

    def propose(self, view: CaseView, now: dt.datetime) -> list[Proposal]:
        if view.outcome is not CaseOutcome.OPEN:
            return []

        proposals: list[Proposal] = []
        if view.contacts_used == 0:
            proposals.append(
                Proposal(
                    case_id=view.case_id,
                    kind=ActionKind.CONTACT,
                    execute_at=now + dt.timedelta(hours=1),
                    note="generic recovery link; no shaping, no cause awareness",
                )
            )
        if self.include_baseline:
            proposals.extend(self._baseline.propose(view, now))
        return proposals


class ArmC:
    """The allocator. Cause-aware, budget-aware, mandate-survival-weighted.

    **Not implemented here.** C3 is hand-authored (CLAUDE.md, "Do not write
    these for me"). This class exists so the comparison harness, the metrics and
    the reporting are already wired for three arms rather than needing a
    retrofit when the allocator lands.

    The seam: implement `propose`, returning `Proposal`s. Available on the view
    are the observed payload (classify it with C2), the rail, the amount, and
    how much of the attempt budget is already spent. Not available, by
    construction, are the true class and the recovery curve.
    """

    name = "C"

    def __init__(self, calendar: ComplianceCalendar) -> None:
        self.calendar = calendar

    def propose(self, view: CaseView, now: dt.datetime) -> list[Proposal]:
        raise NotImplementedError(
            "Arm C is the hand-authored allocator (C3). Implement `propose` in "
            "allocator/ and pass it to run_comparison()."
        )
