"""Port: audit log. Append-only by construction — no update/delete exists."""

from __future__ import annotations

from typing import Protocol

from ..domain.contracts.audit import AuditLog


class AuditLogPort(Protocol):
    async def append(self, event: AuditLog) -> None:
        """Persist one audit event. Must be part of the caller's transaction
        when the event accompanies a task transition."""
        ...

    async def list_by_task(self, task_id: str, *, limit: int = 200) -> list[AuditLog]: ...
