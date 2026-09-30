"""Memory ports: storage (MemoryRepository) and read-only retrieval (MemoryRetriever)."""

from __future__ import annotations

from typing import Protocol

from ..domain.contracts.memory import (
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryStatus,
)


class MemoryRepository(Protocol):
    """Persistence for typed memory items. Status transitions are validated
    by the application service; the repository only applies check-and-set
    updates (expected_from) so terminal states cannot be silently reopened."""

    async def create(self, item: MemoryItem) -> MemoryItem: ...
    async def get(self, item_id: str) -> MemoryItem | None: ...
    async def list(
        self,
        *,
        kinds: frozenset[MemoryKind] | None = ...,
        statuses: frozenset[MemoryStatus] | None = ...,
        limit: int = ...,
        offset: int = ...,
    ) -> list[MemoryItem]: ...
    async def set_status(
        self,
        item_id: str,
        to_status: MemoryStatus,
        *,
        expected_from: frozenset[MemoryStatus],
        superseded_by: str | None = ...,
    ) -> MemoryItem | None:
        """Check-and-set status update. Returns None when no row matched
        (missing id or current status outside expected_from)."""
        ...

    async def clear_superseded_by(self, target_id: str) -> int: ...
    async def purge(self, item_id: str) -> bool:
        """Physical DELETE. Returns True when a row was deleted."""
        ...

    async def count(self, *, statuses: frozenset[MemoryStatus] | None = ...) -> int: ...


class MemoryRetriever(Protocol):
    """Read-only retrieval over the FTS5 index. Never mutates state."""

    async def retrieve(self, query: MemoryQuery) -> list[MemoryHit]: ...
