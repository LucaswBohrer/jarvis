"""Commitment domain contracts (Phase 2, Slice 3).

A commitment is an obligation with its own lifecycle — it is NOT a task.
Tasks are units of execution (7-state machine, Phase 1); commitments are
things the user asked to be charged about ("me cobre de ..."). The two
lifecycles are never mixed: a commitment never becomes a task on its own,
and task states never appear on a commitment.

Hard boundaries (do not loosen without a new architectural decision):
- Statuses are closed: open -> fulfilled / expired / cancelled.
  Terminal states are immutable (D32-style): fulfilled, expired and
  cancelled never transition again.
- Only the sweep marks EXPIRED (open + due_at <= now, idempotent, bounded).
  Only an explicit user command marks FULFILLED or CANCELLED.
- Surfacing (cobrança) is presentation inside the conversation only: it
  never triggers an external action (no webhook, no NEXUS call, no message,
  no task creation). Proven by T18 with exploding mocks.
- due_at = None never expires.
"""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field

from .common import FrozenModel, new_id, utcnow

COMMITMENTS_SCHEMA_VERSION = 2


class CommitmentStatus(str, Enum):
    OPEN = "open"
    FULFILLED = "fulfilled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


#: Terminal states: immutable, never transition again.
COMMITMENT_TERMINAL_STATUSES = frozenset(
    {
        CommitmentStatus.FULFILLED,
        CommitmentStatus.EXPIRED,
        CommitmentStatus.CANCELLED,
    }
)

#: Allowed transitions. EXPIRED is only reachable via the sweep;
#: FULFILLED/CANCELLED only via explicit user command.
_COMMITMENT_TRANSITIONS: dict[CommitmentStatus, frozenset[CommitmentStatus]] = {
    CommitmentStatus.OPEN: frozenset(
        {
            CommitmentStatus.FULFILLED,
            CommitmentStatus.EXPIRED,
            CommitmentStatus.CANCELLED,
        }
    ),
}


def transition_commitment(current: CommitmentStatus, to: CommitmentStatus) -> CommitmentStatus:
    """Pure lifecycle transition. Raises ValueError on any illegal move,
    including any transition out of a terminal state."""
    allowed = _COMMITMENT_TRANSITIONS.get(current, frozenset())
    if to not in allowed:
        raise ValueError(f"illegal commitment transition: {current.value} -> {to.value}")
    return to


class Commitment(FrozenModel):
    """A single commitment (cobrança). Immutable value object; state changes
    go through the CommitmentService with check-and-set updates."""

    schema_version: int = Field(default=COMMITMENTS_SCHEMA_VERSION, ge=COMMITMENTS_SCHEMA_VERSION)
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=2000)
    #: None = no deadline. A commitment without due_at NEVER expires.
    due_at: AwareDatetime | None = None
    status: CommitmentStatus = CommitmentStatus.OPEN
    created_by: str = Field(default="local_user", min_length=1, max_length=64)
    source_message_id: str | None = Field(default=None, max_length=64)
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)
    fulfilled_at: AwareDatetime | None = None
    #: Last time this commitment was surfaced in-conversation. Drives the
    #: 12h dedup window: the same commitment is not charged twice within it.
    last_surfaced_at: AwareDatetime | None = None


class SweepResult(FrozenModel):
    """Outcome of one sweep_commitments() pass. Idempotent: running twice
    in a row expires nothing the second time."""

    schema_version: int = Field(default=COMMITMENTS_SCHEMA_VERSION, ge=COMMITMENTS_SCHEMA_VERSION)
    ran_at: AwareDatetime = Field(default_factory=utcnow)
    #: True when the 1h cooldown suppressed the pass (nothing was scanned).
    cooldown_skipped: bool = False
    expired_commitment_ids: list[str] = Field(default_factory=list)
    expired_memory_ids: list[str] = Field(default_factory=list)


__all__ = [
    "COMMITMENTS_SCHEMA_VERSION",
    "COMMITMENT_TERMINAL_STATUSES",
    "Commitment",
    "CommitmentStatus",
    "SweepResult",
    "transition_commitment",
]
