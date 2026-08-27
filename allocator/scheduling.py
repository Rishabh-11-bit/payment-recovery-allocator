"""Turning an ordinal intent into a compliant timestamp.

Everything here is day arithmetic. No probabilities, and no reading of one:
"later than HIGH would" is an integer offset, and "when money is likely to have
landed" is a list of days-of-month, which is a claim about *when* salaries
arrive rather than about *how likely* recovery is.

Compliance is checked here as well as in the environment. Not redundancy --
proposing something that will be rejected is a decision the allocator failed to
make, and it shows up in the rejection counters as such. An allocator that
leans on the environment to catch it is one that cannot explain its own
behaviour.
"""

from __future__ import annotations

import datetime as dt
from typing import Sequence

from recovery.sim.calendar import IST, ComplianceCalendar

# Margin over the bare PDN lead time. The pre-debit notification has to be sent
# and accepted before the debit; scheduling exactly at the boundary leaves no
# room for the notification itself.
PDN_MARGIN_MINUTES = 15


def earliest_permitted(
    calendar: ComplianceCalendar, now: dt.datetime, rail: str | None = None
) -> dt.datetime:
    """The soonest moment a decision made now could legally execute.

    Rail-specific: UPI needs 25h of notification lead, card 36h. Using a single
    figure schedules every card execution into a window the guard will refuse.
    """
    return now + dt.timedelta(
        hours=calendar.pdn_lead_for(rail), minutes=PDN_MARGIN_MINUTES
    )


def compliant_slot(
    calendar: ComplianceCalendar,
    now: dt.datetime,
    target_day: dt.date,
    rail: str | None = None,
) -> dt.datetime | None:
    """First compliant execution slot on or after `target_day`.

    Returns None when no compliant slot exists, which the caller treats as "not
    today" rather than as an error -- the case stays open and is reconsidered
    tomorrow.
    """
    floor = max(
        dt.datetime.combine(target_day, dt.time(0, 30), tzinfo=IST),
        earliest_permitted(calendar, now, rail),
    )
    slot = calendar.next_compliant_slot(floor)
    while slot is not None:
        violation = calendar.check_attempt(
            decided_at=now, execute_at=slot, attempts_used=0, rail=rail
        )
        if violation is None:
            return slot
        # A UPI slot past the completion deadline is not merely late -- the next
        # legal window is the following morning, so step to it rather than
        # giving up and losing the day.
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


def offset_day(now: dt.datetime, days: int) -> dt.date:
    return (now.astimezone(IST) + dt.timedelta(days=days)).date()


def next_funding_day(
    start: dt.date, funding_days_of_month: Sequence[int], max_wait_days: int
) -> dt.date:
    """First day on or after `start` that falls on a funding day-of-month.

    Falls back to `start` when none is reachable inside `max_wait_days`. The
    fallback matters: waiting past the horizon for a payday that never arrives
    inside it spends the case's remaining life on nothing, and a retry at the
    edge of the window is worth more than a retry that never happens.
    """
    allowed = set(funding_days_of_month)
    for offset in range(max_wait_days + 1):
        candidate = start + dt.timedelta(days=offset)
        if candidate.day in allowed:
            return candidate
    return start
