"""Per-arm metrics.

Reporting discipline, from CLAUDE.md:

* **Money recovered is the headline.** The bar asks for it and it is never
  demoted or omitted.
* **Mandates preserved is a count**, reported alongside -- additive, never a
  replacement for the rupee figure.
* **`terminal_*` counters are definitional**, measured against ground truth:
  P(retry succeeds | expired card, cancelled mandate) = 0, so every attempt and
  contact spent on a TERMINAL failure was structurally incapable of recovering
  anything. This is the claim nobody can dispute without disputing a definition.
* **LTV is not here.** It is a sensitivity swept over remaining-lifetime
  assumptions in C8, never a point estimate. `mandates_preserved` multiplied by
  an assumed lifetime is exactly the cardinal claim dressed as a result that the
  project's credibility depends on not making.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from recovery.models import FailureClass


@dataclass
class ArmMetrics:
    arm: str

    # --- headline ---
    money_recovered_paise: int = 0
    money_at_risk_paise: int = 0

    # --- spend ---
    attempts_spent: int = 0
    contacts_sent: int = 0

    # --- definitional waste (measured against hidden ground truth) ---
    terminal_attempts_wasted: int = 0
    terminal_contacts_sent: int = 0

    # --- mandate survival ---
    mandates_preserved: int = 0
    mandates_revoked: int = 0
    mandates_halted: int = 0

    # --- diagnostics ---
    cases: int = 0
    cases_recovered: int = 0
    proposals_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    attempts_by_class: dict[str, int] = field(default_factory=dict)
    contacts_by_class: dict[str, int] = field(default_factory=dict)
    contact_cost_incurred: float = 0.0

    def record_rejection(self, reason: str) -> None:
        head = reason.split(":", 1)[0]
        self.rejection_reasons[head] = self.rejection_reasons.get(head, 0) + 1
        self.proposals_rejected += 1

    def record_attempt(self, true_class: FailureClass) -> None:
        self.attempts_spent += 1
        key = true_class.value
        self.attempts_by_class[key] = self.attempts_by_class.get(key, 0) + 1
        if true_class is FailureClass.TERMINAL:
            self.terminal_attempts_wasted += 1

    def record_contact(self, true_class: FailureClass, cost: float = 0.0) -> None:
        self.contacts_sent += 1
        self.contact_cost_incurred += cost
        key = true_class.value
        self.contacts_by_class[key] = self.contacts_by_class.get(key, 0) + 1
        if true_class is FailureClass.TERMINAL:
            self.terminal_contacts_sent += 1

    @property
    def recovery_rate(self) -> float:
        return self.cases_recovered / self.cases if self.cases else 0.0

    @property
    def money_recovered_inr(self) -> float:
        return self.money_recovered_paise / 100

    @property
    def attempts_per_case(self) -> float:
        return self.attempts_spent / self.cases if self.cases else 0.0

    @property
    def wasted_attempt_share(self) -> float:
        """Share of the capped budget spent where recovery was impossible."""
        return self.terminal_attempts_wasted / self.attempts_spent if self.attempts_spent else 0.0

    def as_row(self) -> Mapping[str, object]:
        return {
            "arm": self.arm,
            "money_recovered_inr": round(self.money_recovered_inr, 2),
            "attempts_spent": self.attempts_spent,
            "contacts_sent": self.contacts_sent,
            "terminal_attempts_wasted": self.terminal_attempts_wasted,
            "terminal_contacts_sent": self.terminal_contacts_sent,
            "mandates_preserved": self.mandates_preserved,
            "mandates_halted": self.mandates_halted,
            "mandates_revoked": self.mandates_revoked,
        }
