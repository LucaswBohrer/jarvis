"""Local commitment source: reads the JARVIS-local commitments table.

This is the only CommitmentSource implementation in Phase 2. For the local
source the external_id IS the commitments-table id, so the sweep can expire
candidates with check-and-set updates by id. A future external source would
map its own ids and statuses here, in the adapter, never in the core.
"""

from __future__ import annotations

from ...domain.contracts.commitments import CommitmentStatus
from ...ports.commitments import CommitmentSource, ExternalCommitment
from ..persistence.repositories import SqlCommitmentRepository


class LocalCommitmentSource(CommitmentSource):
    source_name = "jarvis-local"

    def __init__(self, commitments: SqlCommitmentRepository) -> None:
        self._commitments = commitments

    async def list_open(self) -> list[ExternalCommitment]:
        items = await self._commitments.list_commitments(
            statuses=frozenset({CommitmentStatus.OPEN}),
            limit=500,
        )
        return [
            ExternalCommitment(
                external_id=item.id,
                title=item.title,
                due_at=item.due_at,
                status=item.status.value,
            )
            for item in items
        ]


__all__ = ["LocalCommitmentSource"]
