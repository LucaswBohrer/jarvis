"""Memory domain contracts (Phase 2, Slice 1).

Typed episodic memory: facts, preferences and project notes that the user
explicitly asks JARVIS to remember. Retrieval is read-only and FTS5-backed.

Hard boundaries (do not loosen without a new architectural decision):
- Memory is DATA, never instruction, never policy, never authority.
- A write only ever happens from an explicit user command
  (origin == "user_explicit_command"); there is no automatic extraction,
  no memory of LLM responses and no memory of NEXUS reads.
- Memory content never becomes a CanonicalFact and the LLM never sets
  confidence; llm_inferred items are never retrievable without confirmation.
"""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field, model_validator

from .common import FrozenModel, new_id, utcnow

MEMORY_SCHEMA_VERSION = 2


class MemoryKind(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    PROJECT_NOTE = "project_note"


class Provenance(str, Enum):
    USER_EXPLICIT = "user_explicit"
    SYSTEM_DERIVED = "system_derived"
    LLM_INFERRED = "llm_inferred"
    IMPORTED = "imported"


class MemoryStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DELETED = "deleted"


#: Statuses that end an item's life. Rows in these statuses can never
#: transition back to active or pending.
TERMINAL_STATUSES = frozenset(
    {MemoryStatus.SUPERSEDED, MemoryStatus.EXPIRED, MemoryStatus.REVOKED, MemoryStatus.DELETED}
)


class Sensitivity(str, Enum):
    STANDARD = "standard"
    SENSITIVE = "sensitive"


class MemoryWriteOp(str, Enum):
    CREATE = "create"
    SUPERSEDE = "supersede"
    REVOKE = "revoke"
    DELETE = "delete"
    CONFIRM = "confirm"
    PURGE = "purge"


class MemoryItem(FrozenModel):
    """A single typed memory record."""

    schema_version: int = Field(default=MEMORY_SCHEMA_VERSION, ge=MEMORY_SCHEMA_VERSION)
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    kind: MemoryKind
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)
    provenance: Provenance
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: Sensitivity = Sensitivity.STANDARD
    status: MemoryStatus = MemoryStatus.PENDING
    session_id: str | None = Field(default=None, max_length=64)
    source_message_id: str | None = Field(default=None, max_length=64)
    superseded_by: str | None = Field(default=None, max_length=64)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _temporal_consistency(self) -> MemoryItem:
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        return self


class MemoryQuery(FrozenModel):
    """Parameters for a read-only retrieval over the memory index."""

    schema_version: int = Field(default=MEMORY_SCHEMA_VERSION, ge=MEMORY_SCHEMA_VERSION)
    query: str = Field(min_length=1, max_length=500)
    kinds: frozenset[MemoryKind] | None = None
    limit: int = Field(default=10, ge=1, le=50)
    time_range: tuple[AwareDatetime, AwareDatetime] | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    include_superseded: bool = False
    include_pending: bool = False
    timeout_ms: int = Field(default=2000, ge=100, le=10000)

    @model_validator(mode="after")
    def _time_range_order(self) -> MemoryQuery:
        if self.time_range is not None and self.time_range[1] <= self.time_range[0]:
            raise ValueError("time_range end must be after start")
        return self


class MemoryHit(FrozenModel):
    """One retrieval result. The snippet is plain text (no FTS markers)."""

    item: MemoryItem
    rank: float
    snippet: str = Field(max_length=2000)


class MemoryWriteResult(FrozenModel):
    schema_version: int = Field(default=MEMORY_SCHEMA_VERSION, ge=MEMORY_SCHEMA_VERSION)
    item_id: str = Field(min_length=1, max_length=64)
    op: MemoryWriteOp
    status: MemoryStatus


class MemoryReadResult(FrozenModel):
    schema_version: int = Field(default=MEMORY_SCHEMA_VERSION, ge=MEMORY_SCHEMA_VERSION)
    hits: tuple[MemoryHit, ...] = ()
    query: str = Field(min_length=1, max_length=500)
