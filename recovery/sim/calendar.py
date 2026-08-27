"""Regulatory time constraints, enforced by the environment on every arm.

Deliberately not enforced inside the arms. Regulation is a property of the
world, not of a policy's good intentions -- an arm that proposes a peak-hour
attempt gets it rejected here, and that rejection is recorded. It also means
Arm C cannot quietly cheat its way to a better number.

Three constraints, all from the NPCI guidelines effective 1 Aug 2025 (press
release 21 May 2025; primary circular not publicly indexed -- cited as
secondary, see CLAUDE.md):

* **Non-peak only.** Autopay executions are barred 10:00-13:00 and
  17:00-21:30 IST.
* **Pre-debit notification, >=24h.** The PDN is a prerequisite: if it fails,
  the debit fails. A PDN sent at or after 23:50 is rejected when the debit date
  is T+1, so the effective cutoff is earlier than a naive 24h subtraction.
* **Attempt cap of 4.** One initial execution plus three retries, ever. Checked
  by the environment, not by the arm.

Bank holidays matter only for Emandate, where the charge day shifts T -> T-1,
or T -> T-3 when both T and T-1 are holidays.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class ComplianceViolation(str):
    """Reason string for a rejected proposal. Kept as text for the audit trail."""


@dataclass(frozen=True)
class ComplianceCalendar:
    peak_windows: tuple[tuple[dt.time, dt.time], ...]
    pdn_lead_time_hours: int
    pdn_cutoff: dt.time
    attempt_cap: int
    bank_holidays: frozenset[dt.date] = frozenset()
    # PDN lead differs by rail: UPI 25h, card 36h. The card figure is longer
    # because the AFA authentication link is valid 72h above the AFA threshold,
    # so the notification has to go out earlier. The flat 24h this started with
    # was wrong for both rails it actually schedules on.
    pdn_lead_by_rail: Mapping[str, int] = field(default_factory=dict)
    # UPI auto-debits must COMPLETE by this time. With peak bars at 10:00-13:00
    # and 17:00-21:30, the legal UPI window is 00:00-10:00 and 13:00-17:00.
    # 21:30-24:00 is outside peak but past the deadline, so an execution
    # scheduled there cannot finish -- a slot that looks legal and is not.
    upi_completion_deadline: dt.time | None = None

    def pdn_lead_for(self, rail: str | None) -> int:
        return self.pdn_lead_by_rail.get(rail or "", self.pdn_lead_time_hours)

    def past_upi_completion_deadline(self, moment: dt.datetime) -> bool:
        if self.upi_completion_deadline is None:
            return False
        return moment.astimezone(IST).time() >= self.upi_completion_deadline

    def is_peak(self, moment: dt.datetime) -> bool:
        clock = moment.astimezone(IST).time()
        return any(start <= clock < end for start, end in self.peak_windows)

    def is_bank_holiday(self, day: dt.date) -> bool:
        # Sundays plus the configured list. Which calendar applies, and whether
        # it varies by bank, is an open question -- see CLAUDE.md "Still open".
        return day.weekday() == 6 or day in self.bank_holidays

    def next_compliant_slot(self, not_before: dt.datetime) -> dt.datetime | None:
        """First non-peak minute at or after `not_before`, searching forward.

        Returns None only if the day is somehow entirely peak, which the
        configured windows make impossible -- but the caller must handle it
        rather than assume.
        """
        moment = not_before.astimezone(IST).replace(second=0, microsecond=0)
        for _ in range(60 * 24 * 2):
            if not self.is_peak(moment):
                return moment
            moment += dt.timedelta(minutes=15)
        return None

    def pdn_deadline_for(
        self, debit_at: dt.datetime, rail: str | None = None
    ) -> dt.datetime:
        """Latest moment a PDN may be sent for a debit at `debit_at`.

        The >=24h lead time, tightened by the 23:50 cutoff rule when the debit
        lands on the following calendar day.
        """
        debit_ist = debit_at.astimezone(IST)
        deadline = debit_ist - dt.timedelta(hours=self.pdn_lead_for(rail))
        cutoff_day = deadline.date()
        if (debit_ist.date() - cutoff_day).days <= 1:
            cutoff = dt.datetime.combine(cutoff_day, self.pdn_cutoff, tzinfo=IST)
            deadline = min(deadline, cutoff - dt.timedelta(minutes=1))
        return deadline

    def check_attempt(
        self,
        *,
        decided_at: dt.datetime,
        execute_at: dt.datetime,
        attempts_used: int,
        rail: str | None = None,
    ) -> ComplianceViolation | None:
        """None if the attempt is permitted, else the reason it is not."""
        if attempts_used >= self.attempt_cap:
            return ComplianceViolation(
                f"attempt_cap_exhausted: {attempts_used}/{self.attempt_cap}"
            )
        if execute_at <= decided_at:
            return ComplianceViolation("execute_at_not_in_future")
        if self.is_peak(execute_at):
            return ComplianceViolation(
                f"peak_hour_barred: {execute_at.astimezone(IST).time()} IST"
            )
        if rail == "upi" and self.past_upi_completion_deadline(execute_at):
            return ComplianceViolation(
                f"past_upi_completion_deadline: must complete by "
                f"{self.upi_completion_deadline} IST"
            )
        if decided_at > self.pdn_deadline_for(execute_at, rail):
            return ComplianceViolation(
                f"pdn_lead_time_unmet: needs >={self.pdn_lead_for(rail)}h "
                f"and a PDN before {self.pdn_cutoff} IST"
            )
        return None

    def shift_for_bank_holidays(self, charge_day: dt.date) -> dt.date:
        """Emandate charge-day shifting: T -> T-1, or T -> T-3 if both are holidays.

        Documented for Emandate only. Card and UPI do not shift.
        """
        if not self.is_bank_holiday(charge_day):
            return charge_day
        previous = charge_day - dt.timedelta(days=1)
        if not self.is_bank_holiday(previous):
            return previous
        return charge_day - dt.timedelta(days=3)


def calendar_from_config(regulatory, bank_holidays: Iterable[dt.date] = ()) -> ComplianceCalendar:
    """Build from the `regulatory` block of `config/default.yaml`.

    The NPCI constants live in config and are read here, not restated.
    """
    return ComplianceCalendar(
        peak_windows=tuple(regulatory.peak_windows_ist),
        pdn_lead_time_hours=regulatory.pdn_lead_time_hours,
        pdn_cutoff=regulatory.pdn_cutoff_ist,
        attempt_cap=regulatory.attempt_cap,
        bank_holidays=frozenset(bank_holidays),
        pdn_lead_by_rail=dict(regulatory.pdn_lead_time_hours_by_rail),
        upi_completion_deadline=regulatory.upi_completion_deadline_ist,
    )
