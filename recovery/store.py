"""SQLite persistence for C1.

Two kinds of table, and the distinction is the point:

* **Append-only** -- `raw_events`, `decisions`, `audit_events`. UPDATE and DELETE
  are blocked by trigger, so immutability is a property of the database rather
  than a convention application code is trusted to honour. A panel asking "what
  stops a later component rewriting a decision" gets a schema-level answer.
* **Mutable projection** -- `cases`, `jobs`. A case's state advances. The audit
  ledger, not the case row, is the record of how it got there.

Exactly-once is enforced by uniqueness constraints (`raw_events.event_id`,
`decisions.idempotency_key`) rather than by read-then-write in Python, which
would race.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import pathlib
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone

from recovery.models import (
    AuditEvent,
    AuditEventType,
    Case,
    CaseState,
    Decision,
    WebhookEnvelope,
)

APPEND_ONLY_TABLES = ("raw_events", "decisions", "audit_events")

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Immutable raw payload store. Written before any parsing, so a payload we
-- cannot yet interpret is still preserved verbatim.
CREATE TABLE IF NOT EXISTS raw_events (
    event_id      TEXT PRIMARY KEY,
    event         TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    received_at   TEXT NOT NULL,
    headers_json  TEXT NOT NULL,
    body_json     TEXT NOT NULL,
    body_sha256   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES raw_events(event_id),
    state      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    UNIQUE (event_id)
);

CREATE TABLE IF NOT EXISTS cases (
    case_id                TEXT PRIMARY KEY,
    chain_key              TEXT NOT NULL UNIQUE,
    order_id               TEXT,
    payment_id             TEXT NOT NULL,
    state                  TEXT NOT NULL,
    opened_at              TEXT NOT NULL,
    last_event_created_at  INTEGER NOT NULL
);

-- Assigns each payment its position in the chain, once, on first sight.
-- attempt_n must be a stable function of the payment rather than a running
-- count taken at decision time: it is part of the idempotency key, so deriving
-- it from "how many decisions exist now" would produce a different key on a
-- replay and defeat the uniqueness constraint that guarantees exactly-once.
CREATE TABLE IF NOT EXISTS chain_attempts (
    chain_key     TEXT NOT NULL,
    payment_id    TEXT NOT NULL,
    attempt_n     INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (chain_key, payment_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    idempotency_key TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    payment_id      TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    attempt_n       INTEGER NOT NULL,
    action          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    decided_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    case_id     TEXT,
    event_id    TEXT,
    event_type  TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_events (case_id, seq);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs (state);
"""

_TRIGGER_TEMPLATE = """
CREATE TRIGGER IF NOT EXISTS {t}_no_update
BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;

CREATE TRIGGER IF NOT EXISTS {t}_no_delete
BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
"""

IMMUTABILITY_TRIGGERS = "".join(
    _TRIGGER_TEMPLATE.format(t=table) for table in APPEND_ONLY_TABLES
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        if str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def initialise(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.executescript(IMMUTABILITY_TRIGGERS)

    def close(self) -> None:
        self._conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ raw --

    def record_raw_event(self, envelope: WebhookEnvelope) -> bool:
        """Persist the payload verbatim. False if this event_id was already seen.

        A duplicate is not an error -- delivery is at-least-once and duplicates
        are expected. The caller records it in the audit trail.
        """
        body_json = json.dumps(envelope.body, sort_keys=True, separators=(",", ":"))
        cursor = self._conn.execute(
            "INSERT INTO raw_events "
            "(event_id, event, created_at, received_at, headers_json, body_json, body_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (event_id) DO NOTHING",
            (
                envelope.event_id,
                envelope.event,
                envelope.created_at,
                _now().isoformat(),
                json.dumps(envelope.headers, sort_keys=True),
                body_json,
                hashlib.sha256(body_json.encode()).hexdigest(),
            ),
        )
        return cursor.rowcount == 1

    def raw_event_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])

    # ----------------------------------------------------------------- jobs --

    def enqueue_job(self, event_id: str) -> bool:
        cursor = self._conn.execute(
            "INSERT INTO jobs (job_id, event_id, state) VALUES (?, ?, 'pending') "
            "ON CONFLICT (event_id) DO NOTHING",
            (f"job_{uuid.uuid4().hex[:16]}", event_id),
        )
        return cursor.rowcount == 1

    def claim_jobs(self, batch_size: int) -> list[sqlite3.Row]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT j.job_id, j.event_id, j.attempts, r.body_json, r.headers_json, "
                "r.created_at FROM jobs j JOIN raw_events r ON r.event_id = j.event_id "
                "WHERE j.state = 'pending' ORDER BY r.created_at, j.rowid LIMIT ?",
                (batch_size,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET state='claimed', claimed_at=?, attempts=attempts+1 "
                    "WHERE job_id=?",
                    (_now().isoformat(), row["job_id"]),
                )
        return list(rows)

    def finish_job(self, job_id: str, state: str) -> None:
        self._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, job_id))

    def pending_job_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
        )

    # ---------------------------------------------------------------- cases --

    def find_case(self, chain_key: str) -> Case | None:
        row = self._conn.execute(
            "SELECT * FROM cases WHERE chain_key = ?", (chain_key,)
        ).fetchone()
        return self._row_to_case(row) if row else None

    def open_case(
        self, chain_key: str, order_id: str | None, payment_id: str, event_created_at: int
    ) -> tuple[Case, bool]:
        """Get-or-create, race-safe. Second element is True if newly opened."""
        cursor = self._conn.execute(
            "INSERT INTO cases (case_id, chain_key, order_id, payment_id, state, opened_at, "
            "last_event_created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (chain_key) DO NOTHING",
            (
                f"case_{uuid.uuid4().hex[:16]}",
                chain_key,
                order_id,
                payment_id,
                CaseState.OPEN.value,
                _now().isoformat(),
                event_created_at,
            ),
        )
        case = self.find_case(chain_key)
        assert case is not None
        return case, cursor.rowcount == 1

    def advance_case(self, case_id: str, state: CaseState, last_event_created_at: int) -> None:
        """Cases are a mutable projection. The audit ledger records the transition."""
        self._conn.execute(
            "UPDATE cases SET state=?, last_event_created_at=? WHERE case_id=?",
            (state.value, last_event_created_at, case_id),
        )

    def case_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0])

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> Case:
        return Case(
            case_id=row["case_id"],
            chain_key=row["chain_key"],
            order_id=row["order_id"],
            payment_id=row["payment_id"],
            state=CaseState(row["state"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            last_event_created_at=row["last_event_created_at"],
        )

    def assign_attempt_n(self, chain_key: str, payment_id: str) -> int:
        """Position of this payment in its chain, assigned once and never changed.

        Idempotent: calling again for the same payment returns the same number,
        which is what keeps the idempotency key stable across replays.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO chain_attempts (chain_key, payment_id, attempt_n, first_seen_at) "
                "SELECT ?, ?, COALESCE(MAX(attempt_n), 0) + 1, ? FROM chain_attempts "
                "WHERE chain_key = ? "
                "ON CONFLICT (chain_key, payment_id) DO NOTHING",
                (chain_key, payment_id, _now().isoformat(), chain_key),
            )
            row = conn.execute(
                "SELECT attempt_n FROM chain_attempts WHERE chain_key=? AND payment_id=?",
                (chain_key, payment_id),
            ).fetchone()
        return int(row["attempt_n"])

    # ------------------------------------------------------------ decisions --

    def record_decision(self, decision: Decision) -> bool:
        """False if a decision already exists for this idempotency key."""
        cursor = self._conn.execute(
            "INSERT INTO decisions (idempotency_key, case_id, payment_id, policy_version, "
            "attempt_n, action, reason, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (
                decision.idempotency_key,
                decision.case_id,
                decision.payment_id,
                decision.policy_version,
                decision.attempt_n,
                decision.action.value,
                decision.reason,
                decision.decided_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def decision_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def decisions_for_case(self, case_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM decisions WHERE case_id=?", (case_id,)).fetchall()
        )

    # ---------------------------------------------------------------- audit --

    def append_audit(
        self,
        event_type: AuditEventType,
        *,
        case_id: str | None = None,
        event_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit_events (at, case_id, event_id, event_type, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _now().isoformat(),
                case_id,
                event_id,
                event_type.value,
                json.dumps(detail or {}, sort_keys=True, default=str),
            ),
        )

    def audit_trail(self, case_id: str | None = None) -> Sequence[AuditEvent]:
        if case_id is None:
            rows = self._conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_events WHERE case_id=? ORDER BY seq", (case_id,)
            ).fetchall()
        return [
            AuditEvent(
                seq=row["seq"],
                at=datetime.fromisoformat(row["at"]),
                case_id=row["case_id"],
                event_id=row["event_id"],
                event_type=AuditEventType(row["event_type"]),
                detail=json.loads(row["detail_json"]),
            )
            for row in rows
        ]
