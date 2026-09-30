"""Audit contracts. Append-only; the application exposes no update/delete."""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field

from ..errors import ErrorCode
from .common import FrozenModel, new_id, utcnow
from .policy import PolicyEffect


class AuditEventType(str, Enum):
    TASK_CREATED = "task.created"
    TASK_PLANNED = "task.planned"
    POLICY_DECIDED = "policy.decided"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    VERIFICATION_COMPLETED = "verification.completed"
    LLM_COMPLETED = "llm.completed"
    LLM_FALLBACK = "llm.fallback"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    MEMORY_CREATED = "memory.created"
    MEMORY_SUPERSEDED = "memory.superseded"
    MEMORY_REVOKED = "memory.revoked"
    MEMORY_DELETED = "memory.deleted"
    MEMORY_PURGED = "memory.purged"
    MEMORY_CONFIRMED = "memory.confirmed"
    MEMORY_WRITE_BLOCKED = "memory.write_blocked"
    MEMORY_EXPORTED = "memory.exported"
    MEMORY_EXPIRED = "memory.expired"
    CONTEXT_ASSEMBLED = "context.assembled"
    COMMITMENT_CREATED = "commitment.created"
    COMMITMENT_WRITE_BLOCKED = "commitment.write_blocked"
    COMMITMENT_FULFILLED = "commitment.fulfilled"
    COMMITMENT_EXPIRED = "commitment.expired"
    COMMITMENT_CANCELLED = "commitment.cancelled"
    COMMITMENT_SURFACED = "commitment.surfaced"
    COMMITMENT_SWEEP = "commitment.sweep"


class AuditActor(str, Enum):
    LOCAL_USER = "local_user"
    SYSTEM = "system"


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    CANCELLED = "cancelled"


class AuditLog(FrozenModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    occurred_at: AwareDatetime = Field(default_factory=utcnow)
    correlation_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    event_type: AuditEventType
    actor: AuditActor = AuditActor.SYSTEM
    capability: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=128)
    decision: PolicyEffect | None = None
    outcome: AuditOutcome
    request_summary: str | None = Field(default=None, max_length=1024)
    result_summary: str | None = Field(default=None, max_length=1024)
    result_digest: str | None = Field(default=None, max_length=128)
    error_code: ErrorCode | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    attempts: int | None = Field(default=None, ge=0)
