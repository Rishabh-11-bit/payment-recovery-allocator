"""C6 -- the audit ledger: a query surface over the append-only trail.

The store already writes every decision and every state transition. That satisfies
"there is an audit trail" and not much else: a trail nobody can read is a
compliance artefact, not an operational one. This module is the reading half.

The bar asks for an audit trail alongside the money figure, and the question a
panel actually asks is **"why did this case get this decision?"** Answering it by
running SQL against `audit_events` proves the data is there; answering it with
one command proves the system can explain itself.

## What a trace has to survive

* **A blocked proposal is part of the story.** The guard's refusals are in the
  trail, so a case that did nothing shows *why* it did nothing. "No decision" and
  "a decision the guard refused" look identical in a decision table and are
  completely different events.
* **Duplicates are signal.** Nine `webhook.duplicate_ignored` entries mean
  at-least-once delivery working as intended, not nine problems.
* **Ordering is the evidence.** The trail is sequenced, and a refresh that
  happened *after* a decision would be a bug the sequence exposes.

Nothing here writes. The ledger is read-only by construction: the tables it reads
block UPDATE and DELETE by trigger, and this module holds no write path at all.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from recovery.models import AuditEvent, AuditEventType
from recovery.store import Store

# Events that mark a decision point, for the compact view.
DECISIVE = {
    AuditEventType.DECISION_RECORDED,
    AuditEventType.DECISION_DUPLICATE_SUPPRESSED,
    AuditEventType.GUARD_BLOCKED,
    AuditEventType.CASE_CLOSED_PAYMENT_RESOLVED,
    AuditEventType.STATE_REFRESH_FAILED,
    AuditEventType.EVENT_STALE_IGNORED,
}

# One-line explanations, so a trace reads as prose rather than as enum values.
NARRATION: Mapping[AuditEventType, str] = {
    AuditEventType.WEBHOOK_RECEIVED: "delivery accepted and queued",
    AuditEventType.WEBHOOK_DUPLICATE_IGNORED: "duplicate delivery, ignored (expected: at-least-once)",
    AuditEventType.WEBHOOK_REJECTED_UNSUPPORTED: "delivery not handled",
    AuditEventType.EVENT_STALE_IGNORED: "older event arrived late; case state not regressed",
    AuditEventType.CASE_OPENED: "case opened on the order's attempt chain",
    AuditEventType.CASE_ATTACHED: "attached to the existing chain",
    AuditEventType.CASE_CLOSED_PAYMENT_RESOLVED: "payment settled; no recovery attempted",
    AuditEventType.STATE_REFRESHED: "authoritative state fetched before deciding",
    AuditEventType.STATE_REFRESH_FAILED: "refresh failed; refused to decide from the payload",
    AuditEventType.FAILURE_NORMALIZED: "key extracted",
    AuditEventType.FAILURE_CLASSIFIED: "classified",
    AuditEventType.FAILURE_UNMAPPED: "no rule matched; resolved through the cost model",
    AuditEventType.FAILURE_SOURCE_UNDOCUMENTED: "source outside its method's documented space",
    AuditEventType.CLASSIFICATION_COST_RESOLVED: "low confidence; class taken from the cost model",
    AuditEventType.GUARD_ALLOWED: "admitted by the guard",
    AuditEventType.GUARD_BLOCKED: "BLOCKED by the guard",
    AuditEventType.DECISION_RECORDED: "decision recorded",
    AuditEventType.DECISION_DUPLICATE_SUPPRESSED: "duplicate decision suppressed",
}


@dataclass(frozen=True)
class DecisionRecord:
    idempotency_key: str
    payment_id: str
    policy_version: str
    attempt_n: int
    action: str
    reason: str
    decided_at: dt.datetime


@dataclass(frozen=True)
class CaseTrace:
    """Everything the ledger knows about one case."""

    case_id: str
    chain_key: str
    order_id: str | None
    payment_id: str
    state: str
    opened_at: dt.datetime
    events: tuple[AuditEvent, ...]
    decisions: tuple[DecisionRecord, ...]
    payments: tuple[str, ...] = ()

    @property
    def blocked(self) -> tuple[AuditEvent, ...]:
        return tuple(e for e in self.events if e.event_type is AuditEventType.GUARD_BLOCKED)

    @property
    def duplicates(self) -> int:
        return sum(
            1
            for e in self.events
            if e.event_type is AuditEventType.WEBHOOK_DUPLICATE_IGNORED
        )

    @property
    def classification(self) -> AuditEvent | None:
        for event in reversed(self.events):
            if event.event_type is AuditEventType.FAILURE_CLASSIFIED:
                return event
        return None

    def outcome_line(self) -> str:
        """One sentence. What happened to this case and why.

        A case with no decision is not the same as a case that was refused one,
        and this is where the difference has to survive.
        """
        if self.decisions:
            latest = self.decisions[-1]
            return f"{latest.action} - {latest.reason}"
        if self.blocked:
            detail = self.blocked[-1].detail
            return (
                f"no obligation created - guard blocked: "
                f"{detail.get('reason', 'unknown')} ({detail.get('detail', '')})"
            )
        for event in reversed(self.events):
            if event.event_type is AuditEventType.CASE_CLOSED_PAYMENT_RESOLVED:
                return "no obligation created - payment had already settled"
            if event.event_type is AuditEventType.STATE_REFRESH_FAILED:
                return "no decision - authoritative state unavailable, refused to guess"
        return "no decision recorded"


class Ledger:
    """Read-only query surface. Holds no write path by construction."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ---------------------------------------------------------------- find --

    def case_ids(self, limit: int | None = None) -> list[str]:
        sql = "SELECT case_id FROM cases ORDER BY opened_at, case_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [row["case_id"] for row in self._store._conn.execute(sql).fetchall()]

    def resolve(self, needle: str) -> str | None:
        """Find a case by its id, its order id, or any payment on its chain.

        Whoever is asking "why did this happen" has whichever identifier the
        complaint arrived with, which is rarely our internal case id.
        """
        conn = self._store._conn
        row = conn.execute(
            "SELECT case_id FROM cases WHERE case_id = ? OR chain_key = ? OR "
            "order_id = ? OR payment_id = ?",
            (needle, needle, needle, needle),
        ).fetchone()
        if row:
            return row["case_id"]
        row = conn.execute(
            "SELECT case_id FROM decisions WHERE payment_id = ? LIMIT 1", (needle,)
        ).fetchone()
        if row:
            return row["case_id"]
        row = conn.execute(
            "SELECT c.case_id FROM cases c JOIN chain_attempts a "
            "ON a.chain_key = c.chain_key WHERE a.payment_id = ? LIMIT 1",
            (needle,),
        ).fetchone()
        return row["case_id"] if row else None

    # --------------------------------------------------------------- trace --

    def trace(self, case_id: str) -> CaseTrace | None:
        conn = self._store._conn
        case = conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if case is None:
            return None

        decisions = tuple(
            DecisionRecord(
                idempotency_key=row["idempotency_key"],
                payment_id=row["payment_id"],
                policy_version=row["policy_version"],
                attempt_n=row["attempt_n"],
                action=row["action"],
                reason=row["reason"],
                decided_at=dt.datetime.fromisoformat(row["decided_at"]),
            )
            for row in conn.execute(
                "SELECT * FROM decisions WHERE case_id = ? ORDER BY attempt_n, decided_at",
                (case_id,),
            ).fetchall()
        )
        payments = tuple(
            row["payment_id"]
            for row in conn.execute(
                "SELECT payment_id FROM chain_attempts WHERE chain_key = ? "
                "ORDER BY attempt_n",
                (case["chain_key"],),
            ).fetchall()
        )
        return CaseTrace(
            case_id=case_id,
            chain_key=case["chain_key"],
            order_id=case["order_id"],
            payment_id=case["payment_id"],
            state=case["state"],
            opened_at=dt.datetime.fromisoformat(case["opened_at"]),
            events=self._events_for(case_id),
            decisions=decisions,
            payments=payments,
        )

    def _events_for(self, case_id: str) -> tuple[AuditEvent, ...]:
        """Case events, plus the pre-case events from the same deliveries.

        `webhook.received`, the duplicates and `payment.state_refreshed` all
        happen before a case exists, so they carry an event id and no case id.
        Reading only by case id loses exactly the part of the story that
        explains where the case came from -- and hides the duplicates entirely,
        which is the evidence that at-least-once delivery was handled.
        """
        events = list(self._store.audit_trail(case_id))
        event_ids = {e.event_id for e in events if e.event_id}
        if not event_ids:
            return tuple(events)

        seen = {e.seq for e in events}
        placeholders = ",".join("?" for _ in event_ids)
        rows = self._store._conn.execute(
            f"SELECT seq FROM audit_events WHERE case_id IS NULL "
            f"AND event_id IN ({placeholders})",
            tuple(event_ids),
        ).fetchall()
        wanted = {row["seq"] for row in rows} - seen
        if wanted:
            for event in self._store.audit_trail():
                if event.seq in wanted:
                    events.append(event)
        return tuple(sorted(events, key=lambda e: e.seq or 0))

    # ------------------------------------------------------------ summary ---

    def summary(self) -> dict[str, int]:
        conn = self._store._conn
        counts = {
            "cases": "SELECT COUNT(*) FROM cases",
            "decisions": "SELECT COUNT(*) FROM decisions",
            "raw_events": "SELECT COUNT(*) FROM raw_events",
            "audit_events": "SELECT COUNT(*) FROM audit_events",
        }
        result = {name: int(conn.execute(sql).fetchone()[0]) for name, sql in counts.items()}
        result["guard_blocks"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
                (AuditEventType.GUARD_BLOCKED.value,),
            ).fetchone()[0]
        )
        return result

    def event_counts(self) -> list[tuple[str, int]]:
        return [
            (row["event_type"], row["n"])
            for row in self._store._conn.execute(
                "SELECT event_type, COUNT(*) n FROM audit_events "
                "GROUP BY event_type ORDER BY n DESC"
            ).fetchall()
        ]

    def block_reasons(self) -> list[tuple[str, int]]:
        """Why the guard refused, aggregated. What the system wanted and could not do."""
        tally: dict[str, int] = {}
        for row in self._store._conn.execute(
            "SELECT detail_json FROM audit_events WHERE event_type = ?",
            (AuditEventType.GUARD_BLOCKED.value,),
        ).fetchall():
            reason = json.loads(row["detail_json"]).get("reason", "unknown")
            tally[reason] = tally.get(reason, 0) + 1
        return sorted(tally.items(), key=lambda item: -item[1])


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def narrate(event: AuditEvent) -> str:
    return NARRATION.get(event.event_type, event.event_type.value)


def render_trace(trace: CaseTrace, *, verbose: bool = False) -> str:
    """The decision trace, as prose a person can read out loud."""
    lines: list[str] = []
    lines.append(f"case {trace.case_id}")
    lines.append(f"  chain      {trace.chain_key}" + ("" if trace.order_id else "   (no order id)"))
    lines.append(f"  state      {trace.state}")
    lines.append(f"  opened     {trace.opened_at:%Y-%m-%d %H:%M:%S} UTC")
    if trace.payments:
        lines.append(
            f"  payments   {len(trace.payments)} on the chain: {', '.join(trace.payments)}"
        )

    classification = trace.classification
    if classification is not None:
        detail = classification.detail
        extra = ""
        if detail.get("cause_family"):
            extra += f", family {detail['cause_family']}"
        if detail.get("deliberately_low_confidence"):
            extra += ", deliberately low confidence"
        if not detail.get("mapped", True):
            extra += ", NO RULE MATCHED"
        lines.append(
            f"  classified {detail.get('class')} at {detail.get('band')} "
            f"(confidence {detail.get('confidence')}){extra}"
        )
        lines.append(f"  key        {detail.get('key')}")

    lines.append("")
    lines.append(f"  OUTCOME    {trace.outcome_line()}")

    if trace.decisions:
        lines.append("")
        lines.append("  decisions:")
        for decision in trace.decisions:
            lines.append(
                f"    attempt {decision.attempt_n}  {decision.action}  "
                f"[{decision.idempotency_key}]"
            )
            lines.append(f"      {decision.reason}")

    if trace.blocked:
        lines.append("")
        lines.append("  blocked by the guard:")
        for event in trace.blocked:
            lines.append(
                f"    {event.detail.get('action')} -> {event.detail.get('reason')}: "
                f"{event.detail.get('detail')}"
            )

    lines.append("")
    shown = trace.events if verbose else tuple(
        e for e in trace.events if e.event_type in DECISIVE or _is_structural(e)
    )
    hidden = len(trace.events) - len(shown)
    lines.append(f"  trail ({len(trace.events)} events" + (f", {hidden} routine hidden" if hidden else "") + "):")
    for event in shown:
        lines.append(
            f"    {event.seq:>4}  {event.at:%H:%M:%S}  {event.event_type.value:<34} "
            f"{narrate(event)}"
        )
    if trace.duplicates and not verbose:
        lines.append(
            f"    ... {trace.duplicates} duplicate deliveries ignored -- "
            "at-least-once working as intended, not a fault"
        )
    return "\n".join(lines)


def _is_structural(event: AuditEvent) -> bool:
    return event.event_type in {
        AuditEventType.CASE_OPENED,
        AuditEventType.STATE_REFRESHED,
        AuditEventType.FAILURE_CLASSIFIED,
        AuditEventType.FAILURE_UNMAPPED,
        AuditEventType.GUARD_ALLOWED,
    }
