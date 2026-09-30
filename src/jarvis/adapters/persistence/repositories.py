"""SQLAlchemy repository implementations. Explicit, no hidden state.

Every mutating method accepts an optional ``session`` so the orchestrator can
bundle a task transition and its audit events in one transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.contracts.audit import AuditLog
from ...domain.contracts.commitments import Commitment, CommitmentStatus
from ...domain.contracts.common import utcnow
from ...domain.contracts.memory import (
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryStatus,
    Provenance,
    Sensitivity,
)
from ...domain.contracts.session import SessionRecord, UserMessage
from ...domain.contracts.task import (
    TERMINAL_STATES,
    TaskInput,
    TaskPlan,
    TaskRecord,
    TaskResult,
)
from ...domain.errors import ErrorCode
from ...ports.repositories import TaskConflictError
from .database import Database
from .fts import build_fts_match_query
from .models import (
    AuditLogRow,
    CommitmentRow,
    MemoryItemRow,
    MessageRow,
    ServiceMetaRow,
    SessionRow,
    TaskRow,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Row <-> contract mapping
# ---------------------------------------------------------------------------


def _insertion_order() -> sa.ColumnElement[int]:
    """Deterministic tiebreak for timestamp ties: SQLite ``rowid``.

    ``occurred_at`` comes from the OS clock, whose granularity on Windows is
    ~15.6 ms — several causally-ordered events routinely share one timestamp.
    The previous tiebreak (``id ASC``) is a random UUID, i.e. arbitrary order.
    Every table here is a rowid table with append-only writes, so ``rowid``
    reproduces the true insertion (causal) order. No migration: rowid is
    intrinsic to the table.
    """
    return sa.column("rowid")


def _session_to_row(s: SessionRecord) -> SessionRow:
    return SessionRow(
        id=s.id,
        status=s.status.value,
        locale=s.locale,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _row_to_session(r: SessionRow) -> SessionRecord:
    return SessionRecord(
        id=r.id,
        status=r.status,  # type: ignore[arg-type]
        locale=r.locale,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _message_to_row(m: UserMessage) -> MessageRow:
    return MessageRow(
        id=m.id,
        session_id=m.session_id,
        task_id=m.task_id,
        role=m.role.value,
        content=m.content,
        idempotency_key=m.idempotency_key,
        created_at=m.created_at,
    )


def _row_to_message(r: MessageRow) -> UserMessage:
    return UserMessage(
        id=r.id,
        session_id=r.session_id,
        task_id=r.task_id,
        role=r.role,  # type: ignore[arg-type]
        content=r.content,
        idempotency_key=r.idempotency_key,
        created_at=r.created_at,
    )


def _task_to_row(t: TaskRecord) -> TaskRow:
    return TaskRow(
        id=t.id,
        session_id=t.session_id,
        kind=t.kind.value,
        state=t.state.value,
        input_json=t.input.model_dump_json(),
        plan_json=t.plan.model_dump_json() if t.plan else None,
        result_json=t.result.model_dump_json() if t.result else None,
        error_code=t.error_code.value if t.error_code else None,
        current_step=t.current_step,
        version=t.version,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _row_to_task(r: TaskRow) -> TaskRecord:
    return TaskRecord(
        id=r.id,
        session_id=r.session_id,
        kind=r.kind,  # type: ignore[arg-type]
        state=r.state,  # type: ignore[arg-type]
        input=TaskInput.model_validate_json(r.input_json),
        plan=TaskPlan.model_validate_json(r.plan_json) if r.plan_json else None,
        result=TaskResult.model_validate_json(r.result_json) if r.result_json else None,
        error_code=ErrorCode(r.error_code) if r.error_code else None,
        current_step=r.current_step,
        version=r.version,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _audit_to_row(a: AuditLog) -> AuditLogRow:
    return AuditLogRow(
        id=a.id,
        occurred_at=a.occurred_at,
        correlation_id=a.correlation_id,
        session_id=a.session_id,
        task_id=a.task_id,
        event_type=a.event_type.value,
        actor=a.actor.value,
        capability=a.capability,
        tool_name=a.tool_name,
        decision=a.decision.value if a.decision else None,
        outcome=a.outcome.value,
        request_summary=a.request_summary,
        result_summary=a.result_summary,
        result_digest=a.result_digest,
        error_code=a.error_code.value if a.error_code else None,
        duration_ms=a.duration_ms,
        attempts=a.attempts,
    )


def _row_to_audit(r: AuditLogRow) -> AuditLog:
    return AuditLog(
        id=r.id,
        occurred_at=r.occurred_at,
        correlation_id=r.correlation_id,
        session_id=r.session_id,
        task_id=r.task_id,
        event_type=r.event_type,  # type: ignore[arg-type]
        actor=r.actor,  # type: ignore[arg-type]
        capability=r.capability,
        tool_name=r.tool_name,
        decision=r.decision,  # type: ignore[arg-type]
        outcome=r.outcome,  # type: ignore[arg-type]
        request_summary=r.request_summary,
        result_summary=r.result_summary,
        result_digest=r.result_digest,
        error_code=ErrorCode(r.error_code) if r.error_code else None,
        duration_ms=r.duration_ms,
        attempts=r.attempts,
    )


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class SqlSessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, session: SessionRecord, *, sa_session: AsyncSession | None = None
    ) -> SessionRecord:
        async def _op(s: AsyncSession) -> SessionRecord:
            s.add(_session_to_row(session))
            await s.flush()
            return session

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._db.session() as s:
            row = await s.get(SessionRow, session_id)
            return _row_to_session(row) if row else None

    async def list_recent(self, *, limit: int = 20) -> list[SessionRecord]:
        """Newest-first session list (F3.7 read-only view).

        Insertion order via rowid (D45): on Windows the OS clock granularity
        collapses causally-ordered creates into one timestamp, so ordering by
        created_at alone is not deterministic.
        """
        async with self._db.session() as s:
            stmt = sa.select(SessionRow).order_by(_insertion_order().desc()).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_session(r) for r in rows]


class SqlMessageRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self, message: UserMessage, *, sa_session: AsyncSession | None = None
    ) -> UserMessage:
        async def _op(s: AsyncSession) -> UserMessage:
            s.add(_message_to_row(message))
            await s.flush()
            return message

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def get(self, message_id: str) -> UserMessage | None:
        async with self._db.session() as s:
            row = await s.get(MessageRow, message_id)
            return _row_to_message(row) if row else None

    async def find_by_idempotency_key(
        self, session_id: str, idempotency_key: str
    ) -> UserMessage | None:
        async with self._db.session() as s:
            stmt = (
                sa.select(MessageRow)
                .where(
                    MessageRow.session_id == session_id,
                    MessageRow.idempotency_key == idempotency_key,
                )
                .limit(1)
            )
            row = (await s.execute(stmt)).scalar_one_or_none()
            return _row_to_message(row) if row else None

    async def list_by_session(self, session_id: str, *, limit: int = 100) -> list[UserMessage]:
        async with self._db.session() as s:
            stmt = (
                sa.select(MessageRow)
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.created_at.asc(), _insertion_order().asc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_message(r) for r in rows]

    async def list_assistant_by_task(self, task_id: str) -> list[UserMessage]:
        async with self._db.session() as s:
            stmt = (
                sa.select(MessageRow)
                .where(MessageRow.task_id == task_id, MessageRow.role == "assistant")
                .order_by(MessageRow.created_at.asc(), _insertion_order().asc())
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_message(r) for r in rows]

    async def set_task_id(
        self,
        message_id: str,
        task_id: str,
        *,
        sa_session: AsyncSession | None = None,
    ) -> None:
        """Link a user message to the task created from it (idempotency replay)."""

        async def _op(s: AsyncSession) -> None:
            stmt = sa.update(MessageRow).where(MessageRow.id == message_id).values(task_id=task_id)
            await s.execute(stmt)
            await s.flush()

        if sa_session is not None:
            await _op(sa_session)
            return
        async with self._db.session() as s, s.begin():
            await _op(s)


class SqlTaskRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, task: TaskRecord, *, sa_session: AsyncSession | None = None
    ) -> TaskRecord:
        async def _op(s: AsyncSession) -> TaskRecord:
            s.add(_task_to_row(task))
            await s.flush()
            return task

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._db.session() as s:
            row = await s.get(TaskRow, task_id)
            return _row_to_task(row) if row else None

    async def update(
        self,
        task: TaskRecord,
        *,
        expected_version: int,
        sa_session: AsyncSession | None = None,
    ) -> TaskRecord:
        """Optimistic-concurrency update. Zero matched rows -> TaskConflictError,
        which means a concurrent modification or a double-execution attempt."""

        async def _op(s: AsyncSession) -> TaskRecord:
            stmt = (
                sa.update(TaskRow)
                .where(TaskRow.id == task.id, TaskRow.version == expected_version)
                .values(
                    state=task.state.value,
                    plan_json=task.plan.model_dump_json() if task.plan else None,
                    result_json=task.result.model_dump_json() if task.result else None,
                    error_code=task.error_code.value if task.error_code else None,
                    current_step=task.current_step,
                    version=task.version,
                    updated_at=task.updated_at,
                )
            )
            result = await s.execute(stmt)
            # UPDATE returns a CursorResult at runtime; the stubs type it as Result.
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise TaskConflictError(
                    f"task {task.id}: expected version {expected_version} not found"
                )
            await s.flush()
            return task

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def list_non_terminal(self, *, limit: int = 200) -> list[TaskRecord]:
        terminal = [st.value for st in TERMINAL_STATES]
        async with self._db.session() as s:
            stmt = (
                sa.select(TaskRow)
                .where(~TaskRow.state.in_(terminal))
                .order_by(TaskRow.created_at.asc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_task(r) for r in rows]

    async def list_recent(self, *, limit: int = 20) -> list[TaskRecord]:
        """Newest-first task list (F3.7 read-only view). rowid ordering (D45)."""
        async with self._db.session() as s:
            stmt = sa.select(TaskRow).order_by(_insertion_order().desc()).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_task(r) for r in rows]


class SqlAuditRepository:
    """Append-only. This class exposes no update or delete — and neither does
    the application: DB triggers reject UPDATE/DELETE on audit_logs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, event: AuditLog, *, sa_session: AsyncSession | None = None) -> None:
        async def _op(s: AsyncSession) -> None:
            s.add(_audit_to_row(event))
            await s.flush()

        if sa_session is not None:
            await _op(sa_session)
            return
        async with self._db.session() as s, s.begin():
            await _op(s)

    async def list_by_task(self, task_id: str, *, limit: int = 200) -> list[AuditLog]:
        async with self._db.session() as s:
            stmt = (
                sa.select(AuditLogRow)
                .where(AuditLogRow.task_id == task_id)
                .order_by(AuditLogRow.occurred_at.asc(), _insertion_order().asc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_audit(r) for r in rows]

    async def list_recent(self, *, limit: int = 50) -> list[AuditLog]:
        """Newest-first audit event list (F3.7 read-only Activity/Audit views).

        Append-only is preserved: this method (like the class) exposes no
        update or delete. Audit rows are already redacted at write time (D6),
        so the full event shape is safe to render.
        """
        async with self._db.session() as s:
            stmt = sa.select(AuditLogRow).order_by(_insertion_order().desc()).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_audit(r) for r in rows]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _affected_rows(result: object) -> int:
    """Rowcount for DML statements. SQLAlchemy types AsyncSession.execute as
    returning Result (no rowcount attribute), but DML execution yields a
    CursorResult at runtime; getattr keeps the access typed."""
    return int(getattr(result, "rowcount", 0) or 0)


def _retrieval_sql(query: MemoryQuery, match: str) -> tuple[str, dict[str, object]]:
    """Build the FTS5 retrieval query. The MATCH operand is the sanitized
    expression from build_fts_match_query; every other parameter is bound."""
    params: dict[str, object] = {
        "match": match,
        "now": _iso(query_now()),
        "limit": query.limit,
    }
    statuses = ["active"]
    if query.include_superseded:
        statuses.append("superseded")
    if query.include_pending:
        statuses.append("pending")
    status_ph = ", ".join(f":st{i}" for i in range(len(statuses)))
    for i, status in enumerate(statuses):
        params[f"st{i}"] = status

    kinds_sql = ""
    if query.kinds:
        kinds_sql = "AND m.kind IN (" + ", ".join(f":k{i}" for i in range(len(query.kinds))) + ") "
        for i, kind in enumerate(sorted(query.kinds, key=lambda k: k.value)):
            params[f"k{i}"] = kind.value

    confidence_sql = ""
    if query.min_confidence is not None:
        confidence_sql = "AND m.confidence >= :min_conf "
        params["min_conf"] = query.min_confidence

    time_sql = ""
    if query.time_range is not None:
        time_sql = "AND m.created_at >= :t0 AND m.created_at <= :t1 "
        params["t0"] = _iso(query.time_range[0])
        params["t1"] = _iso(query.time_range[1])

    # NOTE (S608): the interpolated fragments below are server-generated only —
    # numbered bound-parameter placeholders (":st0") and fixed SQL keywords.
    # Every user-derived value (statuses, kinds, confidence, timestamps,
    # limit, MATCH expression) travels as a bound parameter in `params`.
    sql = f"""
SELECT m.id, m.kind, m.title, m.content, m.provenance, m.confidence,
       m.sensitivity, m.status, m.session_id, m.source_message_id,
       m.superseded_by, m.valid_from, m.valid_until, m.created_at, m.updated_at,
       bm25(memory_fts) AS rank,
       snippet(memory_fts, 2, '', '', ' ... ', 24) AS snippet
FROM memory_fts
JOIN memory_items AS m ON m.id = memory_fts.item_id
WHERE memory_fts MATCH :match
  AND m.status IN ({status_ph})
  AND m.provenance != 'llm_inferred'
  {kinds_sql}{confidence_sql}AND (m.valid_until IS NULL OR m.valid_until > :now)
  {time_sql}ORDER BY rank ASC, m.created_at DESC
LIMIT :limit
"""  # noqa: S608 - only server-generated placeholders/keywords interpolated
    return sql, params


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def query_now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("bool is not a float")
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _row_dict_to_hit(row: Mapping[str, Any]) -> MemoryHit:
    item = MemoryItem(
        id=str(row["id"]),
        kind=MemoryKind(str(row["kind"])),
        title=str(row["title"]),
        content=str(row["content"]),
        provenance=Provenance(str(row["provenance"])),
        confidence=_as_float(row["confidence"]),
        sensitivity=Sensitivity(str(row["sensitivity"])),
        status=MemoryStatus(str(row["status"])),
        session_id=_opt_str(row["session_id"]),
        source_message_id=_opt_str(row["source_message_id"]),
        superseded_by=_opt_str(row["superseded_by"]),
        valid_from=_parse_ts(row["valid_from"]),
        valid_until=_parse_ts(row["valid_until"]),
        created_at=_parse_ts(row["created_at"]) or query_now(),
        updated_at=_parse_ts(row["updated_at"]) or query_now(),
    )
    return MemoryHit(
        item=item,
        rank=_as_float(row["rank"]),
        snippet=str(row["snippet"]),
    )


def _item_to_row(item: MemoryItem) -> MemoryItemRow:
    return MemoryItemRow(
        id=item.id,
        kind=item.kind.value,
        title=item.title,
        content=item.content,
        provenance=item.provenance.value,
        confidence=item.confidence,
        sensitivity=item.sensitivity.value,
        status=item.status.value,
        session_id=item.session_id,
        source_message_id=item.source_message_id,
        superseded_by=item.superseded_by,
        valid_from=item.valid_from,
        valid_until=item.valid_until,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _row_to_item(row: MemoryItemRow) -> MemoryItem:
    return MemoryItem(
        id=row.id,
        kind=MemoryKind(row.kind),
        title=row.title,
        content=row.content,
        provenance=Provenance(row.provenance),
        confidence=row.confidence,
        sensitivity=Sensitivity(row.sensitivity),
        status=MemoryStatus(row.status),
        session_id=row.session_id,
        source_message_id=row.source_message_id,
        superseded_by=row.superseded_by,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlMemoryRepository:
    """MemoryItem storage plus FTS5-backed read-only retrieval."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, item: MemoryItem, *, sa_session: AsyncSession | None = None
    ) -> MemoryItem:
        async def _op(s: AsyncSession) -> MemoryItem:
            s.add(_item_to_row(item))
            await s.flush()
            return item

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def get(self, item_id: str) -> MemoryItem | None:
        async with self._db.session() as s:
            row = await s.get(MemoryItemRow, item_id)
            return _row_to_item(row) if row else None

    async def list_items(
        self,
        *,
        kinds: frozenset[MemoryKind] | None = None,
        statuses: frozenset[MemoryStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryItem]:
        async with self._db.session() as s:
            stmt = sa.select(MemoryItemRow)
            if kinds:
                stmt = stmt.where(MemoryItemRow.kind.in_([k.value for k in kinds]))
            if statuses:
                stmt = stmt.where(MemoryItemRow.status.in_([st.value for st in statuses]))
            stmt = stmt.order_by(MemoryItemRow.created_at.desc()).limit(limit).offset(offset)
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_item(r) for r in rows]

    async def set_status(
        self,
        item_id: str,
        to_status: MemoryStatus,
        *,
        expected_from: frozenset[MemoryStatus],
        superseded_by: str | None = None,
        sa_session: AsyncSession | None = None,
    ) -> MemoryItem | None:
        """Check-and-set: only a row whose status is in expected_from moves.

        Returns the updated item, or None when the row is missing or already
        in a status outside expected_from (e.g. terminal). Terminal states
        can therefore never be silently reopened, even under races.
        """

        async def _op(s: AsyncSession) -> MemoryItem | None:
            stmt = (
                sa.update(MemoryItemRow)
                .where(
                    MemoryItemRow.id == item_id,
                    MemoryItemRow.status.in_([st.value for st in expected_from]),
                )
                .values(
                    status=to_status.value,
                    superseded_by=superseded_by,
                    updated_at=utcnow(),
                )
            )
            result = await s.execute(stmt)
            if _affected_rows(result) == 0:
                return None
            await s.flush()
            row = await s.get(MemoryItemRow, item_id)
            return _row_to_item(row) if row else None

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def expire_due(
        self, now: datetime, limit: int, *, sa_session: AsyncSession | None = None
    ) -> list[str]:
        """Mark active items whose valid_until passed as expired. Bounded,
        idempotent: a second pass finds no candidates. Used by the
        commitment sweep (application/commitments.py)."""

        async def _op(s: AsyncSession) -> list[str]:
            ids_stmt = (
                sa.select(MemoryItemRow.id)
                .where(
                    MemoryItemRow.status == MemoryStatus.ACTIVE.value,
                    MemoryItemRow.valid_until.is_not(None),
                    MemoryItemRow.valid_until <= now,
                )
                .order_by(MemoryItemRow.valid_until.asc())
                .limit(limit)
            )
            ids = list((await s.execute(ids_stmt)).scalars().all())
            if not ids:
                return []
            await s.execute(
                sa.update(MemoryItemRow)
                .where(MemoryItemRow.id.in_(ids))
                .values(status=MemoryStatus.EXPIRED.value, updated_at=utcnow())
            )
            await s.flush()
            return [str(i) for i in ids]

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def clear_superseded_by(
        self, target_id: str, *, sa_session: AsyncSession | None = None
    ) -> int:
        """Break provenance links pointing at target_id (used before purge)."""

        async def _op(s: AsyncSession) -> int:
            stmt = (
                sa.update(MemoryItemRow)
                .where(MemoryItemRow.superseded_by == target_id)
                .values(superseded_by=None, updated_at=utcnow())
            )
            result = await s.execute(stmt)
            return _affected_rows(result)

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def purge(self, item_id: str, *, sa_session: AsyncSession | None = None) -> bool:
        """Physical DELETE. The FTS delete-trigger removes the index row."""

        async def _op(s: AsyncSession) -> bool:
            stmt = sa.delete(MemoryItemRow).where(MemoryItemRow.id == item_id)
            result = await s.execute(stmt)
            return _affected_rows(result) > 0

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def count(self, *, statuses: frozenset[MemoryStatus] | None = None) -> int:
        async with self._db.session() as s:
            stmt = sa.select(sa.func.count()).select_from(MemoryItemRow)
            if statuses:
                stmt = stmt.where(MemoryItemRow.status.in_([st.value for st in statuses]))
            return int((await s.execute(stmt)).scalar_one())

    # -- Retrieval (read-only) ------------------------------------------------

    async def retrieve(self, query: MemoryQuery) -> list[MemoryHit]:
        """FTS5 retrieval. Read-only; on ANY failure returns [] (never raises).

        asyncio.CancelledError is re-raised: cancellation is a control signal,
        not a retrieval failure.
        """
        match = build_fts_match_query(query.query)
        if not match:
            return []
        try:
            async with self._db.session() as s:
                rows = await asyncio.wait_for(
                    self._run_retrieval(s, query, match),
                    timeout=query.timeout_ms / 1000,
                )
                return [_row_dict_to_hit(r) for r in rows]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("memory retrieval failed; returning no hits")
            return []

    async def _run_retrieval(
        self, s: AsyncSession, query: MemoryQuery, match: str
    ) -> list[dict[str, Any]]:
        sql, params = _retrieval_sql(query, match)
        result = await s.execute(sa.text(sql), params)
        return [dict(row) for row in result.mappings().all()]


# ---------------------------------------------------------------------------
# Commitments (Phase 2, Slice 3)
# ---------------------------------------------------------------------------


def _commitment_to_row(c: Commitment) -> CommitmentRow:
    return CommitmentRow(
        id=c.id,
        title=c.title,
        detail=c.detail,
        due_at=c.due_at,
        status=c.status.value,
        created_by=c.created_by,
        source_message_id=c.source_message_id,
        created_at=c.created_at,
        updated_at=c.updated_at,
        fulfilled_at=c.fulfilled_at,
        last_surfaced_at=c.last_surfaced_at,
    )


def _row_to_commitment(r: CommitmentRow) -> Commitment:
    return Commitment(
        id=r.id,
        title=r.title,
        detail=r.detail,
        due_at=r.due_at,
        status=CommitmentStatus(r.status),
        created_by=r.created_by,
        source_message_id=r.source_message_id,
        created_at=r.created_at,
        updated_at=r.updated_at,
        fulfilled_at=r.fulfilled_at,
        last_surfaced_at=r.last_surfaced_at,
    )


class SqlCommitmentRepository:
    """Commitment storage. Status moves are check-and-set: only a row whose
    status is in expected_from moves, so terminal states can never be
    silently reopened, even under races."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, commitment: Commitment, *, sa_session: AsyncSession | None = None
    ) -> Commitment:
        async def _op(s: AsyncSession) -> Commitment:
            s.add(_commitment_to_row(commitment))
            await s.flush()
            return commitment

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def get(self, commitment_id: str) -> Commitment | None:
        async with self._db.session() as s:
            row = await s.get(CommitmentRow, commitment_id)
            return _row_to_commitment(row) if row else None

    async def list_commitments(
        self,
        *,
        statuses: frozenset[CommitmentStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Commitment]:
        async with self._db.session() as s:
            stmt = sa.select(CommitmentRow)
            if statuses:
                stmt = stmt.where(CommitmentRow.status.in_([st.value for st in statuses]))
            stmt = stmt.order_by(CommitmentRow.created_at.desc()).limit(limit).offset(offset)
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_commitment(r) for r in rows]

    async def set_status(
        self,
        commitment_id: str,
        to_status: CommitmentStatus,
        *,
        expected_from: frozenset[CommitmentStatus],
        fulfilled_at: datetime | None = None,
        sa_session: AsyncSession | None = None,
    ) -> Commitment | None:
        """Check-and-set status update. Returns None when the row is missing
        or its status is outside expected_from (e.g. already terminal)."""

        async def _op(s: AsyncSession) -> Commitment | None:
            stmt = (
                sa.update(CommitmentRow)
                .where(
                    CommitmentRow.id == commitment_id,
                    CommitmentRow.status.in_([st.value for st in expected_from]),
                )
                .values(
                    status=to_status.value,
                    fulfilled_at=fulfilled_at,
                    updated_at=utcnow(),
                )
            )
            result = await s.execute(stmt)
            if _affected_rows(result) == 0:
                return None
            await s.flush()
            row = await s.get(CommitmentRow, commitment_id)
            return _row_to_commitment(row) if row else None

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def expire_due(
        self, now: datetime, limit: int, *, sa_session: AsyncSession | None = None
    ) -> list[str]:
        """Mark open commitments with due_at <= now as expired. Bounded and
        idempotent. due_at = NULL never expires."""

        async def _op(s: AsyncSession) -> list[str]:
            ids_stmt = (
                sa.select(CommitmentRow.id)
                .where(
                    CommitmentRow.status == CommitmentStatus.OPEN.value,
                    CommitmentRow.due_at.is_not(None),
                    CommitmentRow.due_at <= now,
                )
                .order_by(CommitmentRow.due_at.asc())
                .limit(limit)
            )
            ids = list((await s.execute(ids_stmt)).scalars().all())
            if not ids:
                return []
            await s.execute(
                sa.update(CommitmentRow)
                .where(CommitmentRow.id.in_(ids))
                .values(status=CommitmentStatus.EXPIRED.value, updated_at=utcnow())
            )
            await s.flush()
            return [str(i) for i in ids]

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def due_for_surfacing(
        self,
        now: datetime,
        horizon: datetime,
        resurface_cutoff: datetime,
        limit: int = 50,
    ) -> list[Commitment]:
        """Open commitments due within the horizon that were not surfaced
        since resurface_cutoff (12h dedup). Read-only."""
        async with self._db.session() as s:
            stmt = (
                sa.select(CommitmentRow)
                .where(
                    CommitmentRow.status == CommitmentStatus.OPEN.value,
                    CommitmentRow.due_at.is_not(None),
                    CommitmentRow.due_at <= horizon,
                    sa.or_(
                        CommitmentRow.last_surfaced_at.is_(None),
                        CommitmentRow.last_surfaced_at <= resurface_cutoff,
                    ),
                )
                .order_by(CommitmentRow.due_at.asc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_row_to_commitment(r) for r in rows]

    async def mark_surfaced(
        self,
        commitment_ids: list[str],
        now: datetime,
        *,
        sa_session: AsyncSession | None = None,
    ) -> int:
        """Stamp last_surfaced_at. Called in the caller's transaction together
        with the commitment.surfaced audit events."""

        async def _op(s: AsyncSession) -> int:
            if not commitment_ids:
                return 0
            result = await s.execute(
                sa.update(CommitmentRow)
                .where(CommitmentRow.id.in_(commitment_ids))
                .values(last_surfaced_at=now, updated_at=utcnow())
            )
            return _affected_rows(result)

        if sa_session is not None:
            return await _op(sa_session)
        async with self._db.session() as s, s.begin():
            return await _op(s)

    async def count(self, *, statuses: frozenset[CommitmentStatus] | None = None) -> int:
        async with self._db.session() as s:
            stmt = sa.select(sa.func.count()).select_from(CommitmentRow)
            if statuses:
                stmt = stmt.where(CommitmentRow.status.in_([st.value for st in statuses]))
            return int((await s.execute(stmt)).scalar_one())


class SqlServiceMetaRepository:
    """Tiny key/value store for durable control state (sweep cooldowns).
    Not application data: keys are fixed, values are opaque strings."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        async with self._db.session() as s:
            row = await s.get(ServiceMetaRow, key)
            return row.value if row else None

    async def set(
        self, key: str, value: str, now: datetime, *, sa_session: AsyncSession | None = None
    ) -> None:
        async def _op(s: AsyncSession) -> None:
            row = await s.get(ServiceMetaRow, key)
            if row is None:
                s.add(ServiceMetaRow(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now
            await s.flush()

        if sa_session is not None:
            await _op(sa_session)
            return
        async with self._db.session() as s, s.begin():
            await _op(s)


__all__ = [
    "SqlAuditRepository",
    "SqlCommitmentRepository",
    "SqlMemoryRepository",
    "SqlMessageRepository",
    "SqlServiceMetaRepository",
    "SqlSessionRepository",
    "SqlTaskRepository",
]
