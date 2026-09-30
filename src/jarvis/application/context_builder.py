"""ContextBuilder (Phase 2, Slice 2).

Assembles an immutable, versioned ContextSnapshot for one task from data
that is ALREADY produced: verified NEXUS facts, retrieved memories,
conversation tail (Slice 3 adds commitments due).

ABSOLUTE RULE: the builder never executes tools and performs no I/O. No
NEXUS calls, no memory writes, no HTTP, no policy evaluation, no database
access, no authorization decisions. The orchestrator fetches every input;
the builder only shapes it. T9 proves this with exploding mocks and a
signature check.

Budgets are chars (§8.3), never tokens. Truncation order is deterministic:
per-section caps first (oldest/worst-rank first, per section rule), then
the total fit in the fixed order memories -> conversation_tail ->
preferences. nexus_facts are NEVER truncated silently: over budget means
llm_permitted=False and the orchestrator skips the LLM (fail-closed,
deterministic fallback).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from pydantic import AwareDatetime

from ..domain.contracts.common import utcnow
from ..domain.contracts.context import (
    LABEL_CONVERSATION_TAIL,
    LABEL_MEMORIES,
    LABEL_PREFERENCES,
    MEMORY_SNIPPET_CHARS,
    TAIL_MESSAGE_LIMIT,
    CommitmentCtx,
    ContextBudgets,
    ContextSection,
    ContextSectionKind,
    ContextSnapshot,
    MemoryCtx,
    MessageCtx,
    compute_snapshot_digest,
    snapshot_content_payload,
)
from ..domain.contracts.llm import DetailLevel
from ..domain.contracts.memory import MemoryHit, Sensitivity
from ..domain.contracts.nexus import CanonicalFact, VerifiedNexusStatus
from ..domain.contracts.session import UserMessage
from ..verification.response import fact_str

_T = TypeVar("_T")


@dataclass(frozen=True)
class ContextBuildInput:
    """Everything the builder needs, already produced. No services, no
    callbacks, no lazy loaders: plain data only."""

    verified: VerifiedNexusStatus | None
    memory_hits: tuple[MemoryHit, ...] = ()
    preference_hits: tuple[MemoryHit, ...] = ()
    #: Oldest -> newest. The builder keeps the last TAIL_MESSAGE_LIMIT.
    tail_messages: tuple[UserMessage, ...] = ()
    #: Commitments due, already filtered by the orchestrator (open, due
    #: within 24h, 12h surfacing dedup). The builder only shapes them.
    commitments_due: tuple[CommitmentCtx, ...] = ()


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


# Deterministic preference -> detail_level rule (D37). This is the ONLY
# permitted influence of memory on the response plan: presentation, never
# facts. Brief hints win over verbose hints; anything else -> None
# (the LLM's own choice, or the fallback default, stands).
_BRIEF_HINTS = (
    "resposta curta",
    "respostas curtas",
    "resumo curto",
    "resumos curtos",
    "seja breve",
    "seja direto",
    "seja objetivo",
    "sem rodeios",
    "vai direto ao ponto",
    "curto e grosso",
    "breve",
    "conciso",
    "sucinto",
)
_VERBOSE_HINTS = (
    "detalhado",
    "detalhada",
    "detalhadamente",
    "completo",
    "completa",
    "longo",
    "longa",
    "com detalhes",
    "bem explicado",
)


def _tail_line(message: MessageCtx) -> str:
    return f"{message.role.value}: {message.content}"


def _memory_line(memory: MemoryCtx) -> str:
    return f"{memory.title} — {memory.snippet}"


def _fact_line(fact: CanonicalFact) -> str:
    return f"{fact.id}: {fact_str(fact.value)}"


def _commitment_line(commitment: CommitmentCtx) -> str:
    return commitment.title


def _chars(lines: Sequence[str]) -> int:
    return sum(len(line) for line in lines)


class _CommitmentBudgetExceeded(Exception):
    """Control flow: the commitments_due section exceeded its char budget.

    Per §8.3 the section is NEVER truncated silently: overflow is an
    internal error (fail-closed). The surfacing query is bounded small so
    this is unreachable in practice."""


class ContextBuilder:
    """Pure assembler. No I/O, no tools, no policy, no network."""

    # -- public API ------------------------------------------------------

    def build(
        self,
        data: ContextBuildInput,
        *,
        task_id: str,
        session_id: str,
        budgets: ContextBudgets | None = None,
        now: AwareDatetime | None = None,
    ) -> ContextSnapshot:
        budgets = budgets or ContextBudgets()

        tail = self._tail_contexts(data.tail_messages)
        preferences = [self._memory_context(hit) for hit in data.preference_hits]
        memories = [self._memory_context(hit) for hit in data.memory_hits]
        commitments_due = list(data.commitments_due)
        facts = list(data.verified.facts) if data.verified is not None else None

        truncated: set[str] = set()

        # Commitments are never truncated, silently or otherwise (§8.3):
        # overflow is an internal error. The surfacing query is bounded
        # small, so this only fires on pathological input.
        commitments_chars = _chars([_commitment_line(c) for c in commitments_due])
        if commitments_chars > budgets.commitments_due:
            raise _CommitmentBudgetExceeded(
                f"commitments_due section over budget: "
                f"{commitments_chars} > {budgets.commitments_due}"
            )

        tail, tail_chars, cut = self._fit_oldest_first(tail, _tail_line, budgets.conversation_tail)
        if cut:
            truncated.add("conversation_tail")
        preferences, prefs_chars, cut = self._fit_oldest_created_first(
            preferences, _memory_line, budgets.preferences
        )
        if cut:
            truncated.add("preferences")
        memories, mems_chars, cut = self._fit_worst_rank_first(
            memories, _memory_line, budgets.memories
        )
        if cut:
            truncated.add("memories")

        facts_chars = _chars([_fact_line(f) for f in facts]) if facts else 0
        llm_permitted = facts is None or facts_chars <= budgets.nexus_facts

        # Total fit, fixed order: memories -> conversation_tail -> preferences.
        # Facts are never touched here (over-budget facts -> llm_permitted=False).
        # Commitments are never dropped either (§8.3).
        total = tail_chars + prefs_chars + mems_chars + facts_chars + commitments_chars
        over = total - budgets.total
        if over > 0:
            original_lens = {
                "memories": len(memories),
                "conversation_tail": len(tail),
                "preferences": len(preferences),
            }
            while over > 0 and memories:
                over -= self._drop_worst_rank(memories, _memory_line)
            if len(memories) < original_lens["memories"]:
                truncated.add("memories")
            while over > 0 and tail:
                over -= self._drop_oldest(tail, _tail_line)
            if len(tail) < original_lens["conversation_tail"]:
                truncated.add("conversation_tail")
            while over > 0 and preferences:
                over -= self._drop_oldest_created(preferences, _memory_line)
            if len(preferences) < original_lens["preferences"]:
                truncated.add("preferences")
            # Recompute applied chars after the total-fit phase.
            tail_chars = _chars([_tail_line(m) for m in tail])
            prefs_chars = _chars([_memory_line(m) for m in preferences])
            mems_chars = _chars([_memory_line(m) for m in memories])

        applied_chars = {
            "conversation_tail": tail_chars,
            "preferences": prefs_chars,
            "memories": mems_chars,
            "commitments_due": commitments_chars,
            "nexus_facts": facts_chars,
        }
        truncated_sections: list[str] = [
            name for name in ("conversation_tail", "preferences", "memories") if name in truncated
        ]

        snapshot = ContextSnapshot(
            task_id=task_id,
            session_id=session_id,
            created_at=now or utcnow(),
            budget_chars=budgets.total,
            conversation_tail=tail,
            preferences=preferences,
            memories=memories,
            commitments_due=commitments_due,
            nexus_facts=facts,
            applied_chars=applied_chars,
            truncated_sections=truncated_sections,
            llm_permitted=llm_permitted,
            digest="0" * 64,  # placeholder, replaced below
        )
        digest = compute_snapshot_digest(snapshot_content_payload(snapshot))
        return snapshot.model_copy(update={"digest": digest})

    def sections_for_llm(self, snapshot: ContextSnapshot) -> tuple[ContextSection, ...]:
        """Labeled sections for the provider-bound LLMRequest.

        Sensitive memories are EXCLUDED here (H10, §80): they stay in the
        local snapshot but never reach an external provider. Empty sections
        are omitted. Fixed order: tail -> preferences -> memories.
        """
        sections: list[ContextSection] = []
        if snapshot.conversation_tail:
            sections.append(
                ContextSection(
                    kind=ContextSectionKind.CONVERSATION_TAIL,
                    label=LABEL_CONVERSATION_TAIL,
                    lines=tuple(_tail_line(m) for m in snapshot.conversation_tail),
                )
            )
        if snapshot.preferences:
            sections.append(
                ContextSection(
                    kind=ContextSectionKind.PREFERENCES,
                    label=LABEL_PREFERENCES,
                    lines=tuple(_memory_line(m) for m in snapshot.preferences),
                )
            )
        public_memories = [
            m for m in snapshot.memories if m.sensitivity is not Sensitivity.SENSITIVE
        ]
        if public_memories:
            sections.append(
                ContextSection(
                    kind=ContextSectionKind.MEMORIES,
                    label=LABEL_MEMORIES,
                    lines=tuple(_memory_line(m) for m in public_memories),
                )
            )
        return tuple(sections)

    @staticmethod
    def preferred_detail_level(preferences: Sequence[MemoryCtx]) -> DetailLevel | None:
        """Deterministic preference -> detail_level mapping.

        THE single permitted influence of memory on the response plan
        (presentation only, never facts). Brief hints win over verbose
        hints; no hint -> None (keep the plan's own level).
        """
        texts = [_normalize(f"{p.title} {p.snippet}") for p in preferences]
        if any(hint in text for text in texts for hint in _BRIEF_HINTS):
            return DetailLevel.BRIEF
        if any(hint in text for text in texts for hint in _VERBOSE_HINTS):
            return DetailLevel.STANDARD
        return None

    # -- shaping (pure) ----------------------------------------------------

    @staticmethod
    def _tail_contexts(messages: Sequence[UserMessage]) -> list[MessageCtx]:
        recent = list(messages[-TAIL_MESSAGE_LIMIT:])
        return [
            MessageCtx(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=m.created_at,
            )
            for m in recent
        ]

    @staticmethod
    def _memory_context(hit: MemoryHit) -> MemoryCtx:
        return MemoryCtx(
            id=hit.item.id,
            kind=hit.item.kind,
            provenance=hit.item.provenance,
            sensitivity=hit.item.sensitivity,
            title=hit.item.title,
            snippet=hit.snippet[:MEMORY_SNIPPET_CHARS],
            confidence=hit.item.confidence,
            created_at=hit.item.created_at,
        )

    # -- truncation (deterministic) ------------------------------------------

    @staticmethod
    def _fit_oldest_first(
        items: list[_T], render: Callable[[_T], str], cap: int
    ) -> tuple[list[_T], int, bool]:
        """Drop from the front (oldest) until the section fits its cap."""
        lines = [render(i) for i in items]
        total = sum(len(line) for line in lines)
        cut = False
        while items and total > cap:
            total -= len(lines.pop(0))
            items.pop(0)
            cut = True
        return items, total, cut

    @staticmethod
    def _fit_oldest_created_first(
        items: list[MemoryCtx], render: Callable[[MemoryCtx], str], cap: int
    ) -> tuple[list[MemoryCtx], int, bool]:
        """Drop the oldest-created memory until the section fits its cap."""
        lines = [render(i) for i in items]
        total = sum(len(line) for line in lines)
        cut = False
        while items and total > cap:
            idx = min(range(len(items)), key=lambda i: (items[i].created_at, -i))
            total -= len(lines.pop(idx))
            items.pop(idx)
            cut = True
        return items, total, cut

    @staticmethod
    def _fit_worst_rank_first(
        items: list[MemoryCtx], render: Callable[[MemoryCtx], str], cap: int
    ) -> tuple[list[MemoryCtx], int, bool]:
        """Drop from the end (worst rank; retrieval is best-first) until fit."""
        lines = [render(i) for i in items]
        total = sum(len(line) for line in lines)
        cut = False
        while items and total > cap:
            total -= len(lines.pop())
            items.pop()
            cut = True
        return items, total, cut

    @staticmethod
    def _drop_worst_rank(items: list[_T], render: Callable[[_T], str]) -> int:
        line = render(items.pop())
        return len(line)

    @staticmethod
    def _drop_oldest(items: list[_T], render: Callable[[_T], str]) -> int:
        line = render(items.pop(0))
        return len(line)

    @staticmethod
    def _drop_oldest_created(items: list[MemoryCtx], render: Callable[[MemoryCtx], str]) -> int:
        idx = min(range(len(items)), key=lambda i: (items[i].created_at, -i))
        line = render(items.pop(idx))
        return len(line)


__all__ = ["ContextBuildInput", "ContextBuilder"]
