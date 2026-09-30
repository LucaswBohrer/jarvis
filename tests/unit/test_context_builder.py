"""Unit tests: ContextBuilder (Phase 2, Slice 2).

T9: the builder runs against pure data while every external operation
(tools, policy, memory I/O, HTTP) is rigged to explode. The builder must
never touch them.
"""

from __future__ import annotations

import inspect

from jarvis.application.context_builder import ContextBuilder, ContextBuildInput
from jarvis.domain.contracts.common import utcnow
from jarvis.domain.contracts.context import (
    LABEL_MEMORIES,
    ContextBudgets,
    ContextSectionKind,
)
from jarvis.domain.contracts.llm import DetailLevel
from jarvis.domain.contracts.memory import (
    MemoryHit,
    MemoryItem,
    MemoryKind,
    Provenance,
    Sensitivity,
)
from jarvis.domain.contracts.nexus import (
    Availability,
    CanonicalFact,
    DataState,
    VerifiedNexusStatus,
)
from jarvis.domain.contracts.session import MessageRole, UserMessage

BUILDER = ContextBuilder()


def _item(
    kind=MemoryKind.FACT,
    title="titulo",
    content="conteudo da memoria",
    sensitivity=Sensitivity.STANDARD,
    provenance=Provenance.USER_EXPLICIT,
    confidence=0.9,
):
    return MemoryItem(
        kind=kind,
        title=title,
        content=content,
        provenance=provenance,
        confidence=confidence,
        sensitivity=sensitivity,
    )


def _hit(item, rank=0.0, snippet=None):
    return MemoryHit(item=item, rank=rank, snippet=snippet or item.content[:200])


def _msg(content, role=MessageRole.USER):
    return UserMessage(session_id="s", role=role, content=content)


def _fact(fid="FACT_VOLTAGE_V", value=220.0):
    return CanonicalFact(id=fid, label=fid.lower(), value=value, unit="V")


def _verified(facts):
    return VerifiedNexusStatus(
        availability=Availability.AVAILABLE, data_state=DataState.FRESH, facts=facts
    )


def _input(**overrides):
    base = {"verified": _verified([_fact()])}
    base.update(overrides)
    return ContextBuildInput(**base)


# -- T9: no tools, no I/O --------------------------------------------------------


def test_t9_builder_never_touches_tools_or_io(monkeypatch):
    """Every external seam explodes; the builder must complete anyway."""

    def _boom(*args, **kwargs):
        raise AssertionError("ContextBuilder attempted an external operation")

    monkeypatch.setattr(
        "jarvis.adapters.persistence.repositories.SqlMemoryRepository.retrieve", _boom
    )
    monkeypatch.setattr(
        "jarvis.adapters.persistence.repositories.SqlMemoryRepository.create", _boom
    )
    monkeypatch.setattr("jarvis.security.policy.PolicyEngine.decide", _boom)
    monkeypatch.setattr("httpx.AsyncClient.post", _boom)
    monkeypatch.setattr("httpx.AsyncClient.get", _boom)

    data = _input(
        memory_hits=(_hit(_item(title="m1")),),
        preference_hits=(_hit(_item(kind=MemoryKind.PREFERENCE, title="p1")),),
        tail_messages=(_msg("oi"), _msg("resposta", role=MessageRole.ASSISTANT)),
    )
    snapshot = BUILDER.build(data, task_id="t", session_id="s", now=utcnow())
    assert snapshot.digest
    assert len(BUILDER.sections_for_llm(snapshot)) == 3


def test_t9_builder_signature_takes_only_data():
    """Structural proof: build() accepts data + ids + budgets, no services."""
    params = list(inspect.signature(ContextBuilder.build).parameters)
    assert params == ["self", "data", "task_id", "session_id", "budgets", "now"]


# -- snapshot shape --------------------------------------------------------------


def test_build_snapshot_sections_order_and_labels():
    snapshot = BUILDER.build(
        _input(
            memory_hits=(_hit(_item(title="mem")),),
            preference_hits=(_hit(_item(kind=MemoryKind.PREFERENCE, title="pref")),),
            tail_messages=(_msg("a"), _msg("b", role=MessageRole.ASSISTANT)),
        ),
        task_id="t",
        session_id="s",
        now=utcnow(),
    )
    sections = BUILDER.sections_for_llm(snapshot)
    assert [s.kind for s in sections] == [
        ContextSectionKind.CONVERSATION_TAIL,
        ContextSectionKind.PREFERENCES,
        ContextSectionKind.MEMORIES,
    ]
    assert sections[2].label == LABEL_MEMORIES
    # Tail stays oldest -> newest.
    assert sections[0].lines[0].startswith("user: a")
    assert sections[0].lines[1].startswith("assistant: b")
    # Memories are labeled NOT-verified, never mixed into facts.
    assert snapshot.memories[0].verified is False
    assert snapshot.nexus_facts is not None and len(snapshot.nexus_facts) == 1


def test_tail_keeps_only_last_20_messages():
    tail = tuple(_msg(f"m{i}") for i in range(30))
    snapshot = BUILDER.build(_input(tail_messages=tail), task_id="t", session_id="s")
    assert len(snapshot.conversation_tail) == 20
    assert snapshot.conversation_tail[0].content == "m10"
    assert snapshot.conversation_tail[-1].content == "m29"


def test_sensitive_memories_excluded_from_llm_sections():
    snapshot = BUILDER.build(
        _input(
            memory_hits=(
                _hit(_item(title="publica")),
                _hit(_item(title="segredo", sensitivity=Sensitivity.SENSITIVE)),
            )
        ),
        task_id="t",
        session_id="s",
    )
    # Both live in the local snapshot...
    assert len(snapshot.memories) == 2
    # ...but only the standard one reaches the provider-bound sections.
    sections = BUILDER.sections_for_llm(snapshot)
    mem_section = next(s for s in sections if s.kind is ContextSectionKind.MEMORIES)
    assert len(mem_section.lines) == 1
    assert "publica" in mem_section.lines[0]
    blob = "\n".join(line for s in sections for line in s.lines)
    assert "segredo" not in blob


def test_empty_sections_are_omitted():
    snapshot = BUILDER.build(_input(), task_id="t", session_id="s")
    assert BUILDER.sections_for_llm(snapshot) == ()


def test_unavailable_nexus_keeps_facts_none():
    snapshot = BUILDER.build(_input(verified=None), task_id="t", session_id="s")
    assert snapshot.nexus_facts is None
    assert snapshot.llm_permitted is True


# -- budgets ---------------------------------------------------------------------


def test_tail_truncation_drops_oldest_first():
    tail = tuple(_msg("x" * 100) for _ in range(10))  # ~100 chars/line
    budgets = ContextBudgets(conversation_tail=250)
    snapshot = BUILDER.build(
        _input(tail_messages=tail, verified=None),
        task_id="t",
        session_id="s",
        budgets=budgets,
    )
    assert snapshot.applied_chars["conversation_tail"] <= 250
    assert snapshot.truncated_sections == ["conversation_tail"]
    # Newest messages survive.
    assert snapshot.conversation_tail[-1].content == "x" * 100
    assert len(snapshot.conversation_tail) == 2


def test_memories_truncation_drops_worst_rank_first():
    hits = tuple(
        _hit(_item(title=f"memoria numero {i}", content="y" * 60), rank=float(i)) for i in range(6)
    )
    budgets = ContextBudgets(memories=200)
    snapshot = BUILDER.build(
        _input(memory_hits=hits, verified=None),
        task_id="t",
        session_id="s",
        budgets=budgets,
    )
    assert snapshot.applied_chars["memories"] <= 200
    assert snapshot.truncated_sections == ["memories"]
    # Best-ranked (first) memories survive.
    assert snapshot.memories[0].title == "memoria numero 0"


def test_total_fit_truncates_in_fixed_order():
    hits = tuple(_hit(_item(title=f"m{i}", content="z" * 100)) for i in range(10))
    tail = tuple(_msg("ok") for _ in range(3))  # tiny: ~9 chars/line
    budgets = ContextBudgets(
        total=500,
        conversation_tail=100_000,
        preferences=100_000,
        memories=100_000,
        nexus_facts=100_000,
    )
    snapshot = BUILDER.build(
        _input(memory_hits=hits, tail_messages=tail, verified=None),
        task_id="t",
        session_id="s",
        budgets=budgets,
    )
    total = sum(snapshot.applied_chars.values())
    assert total <= 500
    # Memories are sacrificed first; the tail survives untouched.
    assert "memories" in snapshot.truncated_sections
    assert "conversation_tail" not in snapshot.truncated_sections
    assert len(snapshot.conversation_tail) == 3


def test_facts_over_budget_skips_llm_fail_closed():
    facts = [_fact(f"FACT_X{i}", i) for i in range(50)]
    budgets = ContextBudgets(nexus_facts=100)
    snapshot = BUILDER.build(
        _input(verified=_verified(facts)),
        task_id="t",
        session_id="s",
        budgets=budgets,
    )
    assert snapshot.llm_permitted is False
    # Facts are never truncated silently: all 50 remain in the snapshot.
    assert snapshot.nexus_facts is not None and len(snapshot.nexus_facts) == 50
    assert snapshot.applied_chars["nexus_facts"] > 100


def test_facts_within_budget_permit_llm():
    snapshot = BUILDER.build(
        _input(verified=_verified([_fact()])),
        task_id="t",
        session_id="s",
    )
    assert snapshot.llm_permitted is True


# -- preference -> detail_level ----------------------------------------------------


def _pref_ctx(title, content):
    return BUILDER._memory_context(
        _hit(_item(kind=MemoryKind.PREFERENCE, title=title, content=content))
    )


def test_preferred_detail_level_brief_wins():
    assert ContextBuilder.preferred_detail_level([]) is None
    assert ContextBuilder.preferred_detail_level([_pref_ctx("estilo", "sem preferencia")]) is None

    brief = [_pref_ctx("estilo", "prefiro resposta curta")]
    assert ContextBuilder.preferred_detail_level(brief) is DetailLevel.BRIEF

    verbose = [_pref_ctx("estilo", "explique detalhadamente")]
    assert ContextBuilder.preferred_detail_level(verbose) is DetailLevel.STANDARD

    # Brief wins over verbose when both match.
    assert ContextBuilder.preferred_detail_level(brief + verbose) is DetailLevel.BRIEF


def test_digest_stable_for_identical_content():
    kwargs = {"task_id": "t", "session_id": "s", "now": utcnow()}
    data = _input(
        memory_hits=(_hit(_item(title="m")),),
        tail_messages=(_msg("oi"),),
    )
    s1 = BUILDER.build(data, **kwargs)
    s2 = BUILDER.build(data, **kwargs)
    assert s1.digest == s2.digest
