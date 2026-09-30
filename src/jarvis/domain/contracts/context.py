"""Context domain contracts (Phase 2, Slice 2).

A ContextSnapshot is the per-task, immutable, versioned view the LLM may
see: verified NEXUS facts + retrieved memories (labeled NOT-verified) +
conversation tail + (Slice 3: commitments due).

Hard boundaries (do not loosen without a new architectural decision):
- MemoryCtx.verified is ALWAYS False: a memory is a past user statement,
  never a CanonicalFact. verify_plan keeps rejecting anything that is not
  a verified fact id.
- sensitivity=sensitive memories live in the snapshot (local) but are
  EXCLUDED from the sections sent to an external provider (H10, §80).
- The snapshot carries a deterministic SHA-256 digest over its content
  (ids and timestamps excluded): same content -> same digest.
- ContextBuilder never executes tools; it only shapes already-produced data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from .common import FrozenModel, new_id, utcnow
from .memory import MemoryKind, Provenance, Sensitivity
from .nexus import CanonicalFact
from .session import MessageRole

CONTEXT_SCHEMA_VERSION = 2

#: Hard cap on the snippet carried per memory into the snapshot.
MEMORY_SNIPPET_CHARS = 400

#: How many of the most recent session messages the tail may hold.
TAIL_MESSAGE_LIMIT = 20

#: Section labels sent to the LLM. Memories are labeled as NOT-verified
#: past statements, verbatim from the architecture review (§8.2).
LABEL_CONVERSATION_TAIL = (
    "[CONVERSA RECENTE — últimas mensagens desta sessão, da mais antiga para a mais nova.]"
)
LABEL_PREFERENCES = (
    "[PREFERÊNCIAS DO USUÁRIO — como você gosta de ser atendido. "
    "Podem influenciar o nível de detalhe da resposta, nunca os fatos.]"
)
LABEL_MEMORIES = (
    "[MEMÓRIAS DO USUÁRIO — declarações passadas, NÃO verificadas.\n"
    "Podem estar desatualizadas. Nunca as trate como fatos medidos.]"
)


class MessageCtx(FrozenModel):
    """One conversation-tail message, oldest -> newest, never reordered."""

    id: str = Field(min_length=1, max_length=64)
    role: MessageRole
    content: str = Field(min_length=1, max_length=2000)
    created_at: AwareDatetime


class MemoryCtx(FrozenModel):
    """Lean projection of a MemoryHit for the snapshot.

    verified is pinned False: this is a past user statement, NOT a measured
    fact. sensitivity rides along so the builder can exclude sensitive
    memories from the provider-bound sections (§8.5).
    """

    id: str = Field(min_length=1, max_length=64)
    kind: MemoryKind
    provenance: Provenance
    sensitivity: Sensitivity = Sensitivity.STANDARD
    title: str = Field(min_length=1, max_length=120)
    snippet: str = Field(min_length=1, max_length=MEMORY_SNIPPET_CHARS)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: AwareDatetime
    #: Pinned False at the type level: a memory is a past user statement,
    #: never a measured fact. Any attempt to construct verified=True fails
    #: validation.
    verified: Literal[False] = False


class CommitmentCtx(FrozenModel):
    """One due commitment for the snapshot's commitments_due section.

    Slice 3: surfaced in-conversation as the deterministic "📌 Lembretes"
    block. Presentation only: surfacing never executes an external action.
    """

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    due_at: AwareDatetime | None = None
    #: True when due_at is in the past and the commitment is still open
    #: (the sweep had not run yet when the snapshot was built).
    overdue: bool = False


class ContextSectionKind(str, Enum):
    CONVERSATION_TAIL = "conversation_tail"
    PREFERENCES = "preferences"
    MEMORIES = "memories"


class ContextSection(FrozenModel):
    """One labeled section of the snapshot, as sent to the LLM provider."""

    kind: ContextSectionKind
    label: str = Field(min_length=1, max_length=500)
    lines: tuple[str, ...] = Field(default=())


class ContextBudgets(FrozenModel):
    """Char budgets (§8.3). No token counting, no new dependency (§78)."""

    total: int = Field(default=12000, ge=100, le=100000)
    conversation_tail: int = Field(default=4000, ge=100, le=100000)
    preferences: int = Field(default=2000, ge=100, le=100000)
    memories: int = Field(default=4000, ge=100, le=100000)
    commitments_due: int = Field(default=1000, ge=100, le=100000)  # Slice 3
    nexus_facts: int = Field(default=8000, ge=100, le=100000)


class ContextSnapshot(FrozenModel):
    """Immutable, versioned, identifiable per-task context.

    Slice 3 adds commitments_due: open commitments due within 24h that were
    not surfaced in the last 12h (dedup via last_surfaced_at)."""

    schema_version: int = Field(default=CONTEXT_SCHEMA_VERSION, ge=CONTEXT_SCHEMA_VERSION)
    snapshot_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    created_at: AwareDatetime = Field(default_factory=utcnow)
    budget_chars: int = Field(ge=100, le=100000)
    conversation_tail: list[MessageCtx] = Field(default_factory=list)
    preferences: list[MemoryCtx] = Field(default_factory=list)
    memories: list[MemoryCtx] = Field(default_factory=list)
    #: Slice 3. Rendered deterministically as "📌 Lembretes"; never sent to
    #: tools, never an external action.
    commitments_due: list[CommitmentCtx] = Field(default_factory=list)
    nexus_facts: list[CanonicalFact] | None = None
    applied_chars: dict[str, int] = Field(default_factory=dict)
    truncated_sections: list[str] = Field(default_factory=list)
    #: False when nexus_facts exceeded their budget: the facts are kept in
    #: the (local) snapshot but the LLM stage MUST be skipped (fail-closed,
    #: deterministic fallback). Facts are never truncated silently.
    llm_permitted: bool = True
    #: SHA-256 over the canonical content (below). Deterministic: same
    #: content -> same digest, regardless of ids/timestamps.
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _content_only(item: MessageCtx | MemoryCtx) -> dict[str, Any]:
    """Content without random identity: uuid ids and timestamps excluded."""
    data = item.model_dump(mode="json")
    data.pop("id", None)
    data.pop("created_at", None)
    return data


def _content_only_commitment(item: CommitmentCtx) -> dict[str, Any]:
    data = item.model_dump(mode="json")
    data.pop("id", None)
    return data


def snapshot_content_payload(snapshot: ContextSnapshot) -> dict[str, Any]:
    """The exact content covered by the digest.

    Random identity (message/memory uuid ids, timestamps) is excluded so
    identical content yields identical digests. CanonicalFact ids are
    semantic (FACT_*) and ARE included.
    """
    return {
        "budget_chars": snapshot.budget_chars,
        "conversation_tail": [_content_only(m) for m in snapshot.conversation_tail],
        "preferences": [_content_only(m) for m in snapshot.preferences],
        "memories": [_content_only(m) for m in snapshot.memories],
        "commitments_due": [_content_only_commitment(c) for c in snapshot.commitments_due],
        "nexus_facts": (
            [f.model_dump(mode="json") for f in snapshot.nexus_facts]
            if snapshot.nexus_facts is not None
            else None
        ),
        "applied_chars": dict(sorted(snapshot.applied_chars.items())),
        "truncated_sections": list(snapshot.truncated_sections),
        "llm_permitted": snapshot.llm_permitted,
    }


def compute_snapshot_digest(payload: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 over the canonical JSON of the payload."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
