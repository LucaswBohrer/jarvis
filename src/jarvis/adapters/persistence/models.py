"""SQLAlchemy 2.x models for the JARVIS-local SQLite database.

Tables: sessions, messages, tasks, audit_logs (Phase 1) plus memory_items,
commitments and service_meta (Phase 2). Timestamps are stored as ISO-8601 UTC
text via UTCDateTime (deterministic, no SQLite datetime quirks). JSON columns
hold versioned Pydantic envelopes, never arbitrary dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store tz-aware datetimes as ISO-8601 UTC text. Naive datetimes rejected."""

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is forbidden in JARVIS persistence")
        return value.astimezone(UTC).isoformat()

    def process_result_value(self, value: str | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    status: Mapped[str] = mapped_column(
        sa.Text, sa.CheckConstraint("status IN ('active','closed')"), nullable=False
    )
    locale: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    role: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("role IN ('user','assistant','system')"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        sa.Index("ix_messages_session_created", "session_id", "created_at"),
        # Partial unique index: idempotency is per-session, null keys unrestricted.
        sa.Index(
            "uq_messages_session_idemkey",
            "session_id",
            "idempotency_key",
            unique=True,
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        ),
    )


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint(
            "state IN ('pending','planning','running','waiting_confirmation',"
            "'completed','failed','cancelled')"
        ),
        nullable=False,
    )
    input_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    plan_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    current_step: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        sa.Index("ix_tasks_session_created", "session_id", "created_at"),
        sa.Index("ix_tasks_state", "state"),
    )


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    correlation_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor: Mapped[str] = mapped_column(sa.Text, nullable=False)
    capability: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    outcome: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    attempts: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    __table_args__ = (
        sa.Index("ix_audit_task_occurred", "task_id", "occurred_at"),
        sa.Index("ix_audit_correlation", "correlation_id"),
    )


class MemoryItemRow(Base):
    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    kind: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("kind IN ('fact', 'preference', 'project_note')"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("length(title) BETWEEN 1 AND 120"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("length(content) BETWEEN 1 AND 4000"),
        nullable=False,
    )
    provenance: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint(
            "provenance IN ('user_explicit', 'system_derived', 'llm_inferred', 'imported')"
        ),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(
        sa.Float,
        sa.CheckConstraint("confidence >= 0.0 AND confidence <= 1.0"),
        nullable=False,
    )
    sensitivity: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("sensitivity IN ('standard', 'sensitive')"),
        nullable=False,
        server_default="standard",
    )
    status: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'superseded', 'expired', 'revoked', 'deleted')"
        ),
        nullable=False,
        server_default="pending",
    )
    session_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    source_message_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    superseded_by: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("memory_items.id"), nullable=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        sa.Index("ix_memory_kind_status", "kind", "status"),
        sa.Index("ix_memory_created", "created_at"),
        sa.Index(
            "ix_memory_valid_until",
            "valid_until",
            sqlite_where=sa.text("status = 'active' AND valid_until IS NOT NULL"),
        ),
    )


class CommitmentRow(Base):
    __tablename__ = "commitments"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    title: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("length(title) BETWEEN 1 AND 200"),
        nullable=False,
    )
    detail: Mapped[str | None] = mapped_column(
        sa.Text,
        sa.CheckConstraint("detail IS NULL OR length(detail) <= 2000"),
        nullable=True,
    )
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        sa.Text,
        sa.CheckConstraint("status IN ('open', 'fulfilled', 'expired', 'cancelled')"),
        nullable=False,
        server_default="open",
    )
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="local_user")
    source_message_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    fulfilled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_surfaced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (sa.Index("ix_commitments_status_due", "status", "due_at"),)


class ServiceMetaRow(Base):
    __tablename__ = "service_meta"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[str] = mapped_column(sa.Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
