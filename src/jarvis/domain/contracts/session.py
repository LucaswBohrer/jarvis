"""Session and message contracts. Phase 1 is single-user; the contracts are
shaped so identity can later evolve (User -> Identity -> Session) without
rewriting task orchestration."""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field, field_validator

from .common import FrozenModel, new_id, utcnow


class SessionStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SessionCreate(FrozenModel):
    locale: str = Field(default="pt-BR", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")


class SessionRecord(FrozenModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    status: SessionStatus = SessionStatus.ACTIVE
    locale: str = Field(default="pt-BR", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)


MAX_MESSAGE_CHARS = 2000


class UserMessage(FrozenModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    role: MessageRole = MessageRole.USER
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    idempotency_key: str | None = Field(default=None, max_length=128)
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @field_validator("content")
    @classmethod
    def _normalize(cls, value: str) -> str:
        # Collapse whitespace runs; keep user text otherwise intact.
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("message must contain non-whitespace text")
        return normalized
