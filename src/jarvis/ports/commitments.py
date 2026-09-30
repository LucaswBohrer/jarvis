"""Commitment source port: the seam for future external integrations.

Phase 2 (Slice 3) ships only LocalCommitmentSource (the JARVIS-local
commitments table). A CloneCobradorSource is deliberately NOT implemented:
there is no documented HTTP endpoint, schema, auth method or protocol to
build against, and shipping a stub that pretends to integrate would be
dishonest. The unknowns are recorded in docs/PHASE2_ARCHITECTURE_REVIEW.md
§10.2. When (and if) an API exists, it plugs in here without touching the
core: the sweep lists candidates through this port.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypedDict


class ExternalCommitment(TypedDict):
    """One open commitment as reported by a source, in the source's own
    vocabulary. The adapter maps it; the core never trusts it blindly."""

    external_id: str
    title: str
    due_at: datetime | None
    status: str


class CommitmentSource(Protocol):
    """Lists open commitments from one origin."""

    source_name: str

    async def list_open(self) -> list[ExternalCommitment]: ...


__all__ = ["CommitmentSource", "ExternalCommitment"]
