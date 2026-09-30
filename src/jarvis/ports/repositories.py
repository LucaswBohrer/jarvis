"""Ports: persistence repositories. Explicit operations, no hidden state."""

from __future__ import annotations

from typing import Protocol

from ..domain.contracts.session import SessionRecord, UserMessage
from ..domain.contracts.task import TaskRecord


class SessionRepository(Protocol):
    async def create(self, session: SessionRecord) -> SessionRecord: ...
    async def get(self, session_id: str) -> SessionRecord | None: ...
    async def close(self, session_id: str) -> None: ...


class MessageRepository(Protocol):
    async def add(self, message: UserMessage) -> UserMessage: ...
    async def get(self, message_id: str) -> UserMessage | None: ...
    async def find_by_idempotency_key(
        self, session_id: str, idempotency_key: str
    ) -> UserMessage | None: ...
    async def list_by_session(self, session_id: str, *, limit: int = 100) -> list[UserMessage]: ...
    async def list_assistant_by_task(self, task_id: str) -> list[UserMessage]: ...


class TaskConflictError(Exception):
    """Optimistic-concurrency conflict: expected version did not match."""


class TaskRepository(Protocol):
    async def create(self, task: TaskRecord) -> TaskRecord: ...
    async def get(self, task_id: str) -> TaskRecord | None: ...
    async def update(self, task: TaskRecord, *, expected_version: int) -> TaskRecord:
        """Persist a transitioned task.

        Applies ``WHERE id=? AND version=?`` with expected_version; raises
        TaskConflictError when no row matches (concurrent modification or
        double execution attempt).
        """
        ...

    async def list_non_terminal(self, *, limit: int = 200) -> list[TaskRecord]:
        """For crash recovery at startup."""
        ...
