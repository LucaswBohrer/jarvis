"""Unit tests: context domain contracts (Phase 2, Slice 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jarvis.domain.contracts.common import utcnow
from jarvis.domain.contracts.context import (
    LABEL_CONVERSATION_TAIL,
    LABEL_MEMORIES,
    LABEL_PREFERENCES,
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
from jarvis.domain.contracts.memory import (
    MemoryKind,
    Provenance,
    Sensitivity,
)
from jarvis.domain.contracts.nexus import CanonicalFact
from jarvis.domain.contracts.session import MessageRole


def _memory_ctx(**overrides):
    base = {
        "id": "m1",
        "kind": MemoryKind.FACT,
        "provenance": Provenance.USER_EXPLICIT,
        "sensitivity": Sensitivity.STANDARD,
        "title": "titulo",
        "snippet": "trecho",
        "confidence": 0.9,
        "created_at": utcnow(),
    }
    base.update(overrides)
    return MemoryCtx(**base)


def _message_ctx(**overrides):
    base = {
        "id": "msg1",
        "role": MessageRole.USER,
        "content": "oi",
        "created_at": utcnow(),
    }
    base.update(overrides)
    return MessageCtx(**base)


def test_memory_label_marks_memories_not_verified_verbatim():
    assert "NÃO verificadas" in LABEL_MEMORIES
    assert "nunca as trate como fatos medidos" in LABEL_MEMORIES.lower()


def test_labels_are_non_empty_strings():
    assert LABEL_CONVERSATION_TAIL and LABEL_PREFERENCES and LABEL_MEMORIES


def test_memory_ctx_verified_pinned_false():
    ctx = _memory_ctx()
    assert ctx.verified is False
    with pytest.raises(ValidationError):
        _memory_ctx(verified=True)


def test_memory_ctx_carries_sensitivity_default_standard():
    assert _memory_ctx().sensitivity is Sensitivity.STANDARD
    assert _memory_ctx(sensitivity=Sensitivity.SENSITIVE).sensitivity is Sensitivity.SENSITIVE


def test_memory_ctx_snippet_capped():
    with pytest.raises(ValidationError):
        _memory_ctx(snippet="x" * 401)


def test_context_budgets_defaults():
    budgets = ContextBudgets()
    assert budgets.total == 12000
    assert budgets.conversation_tail == 4000
    assert budgets.preferences == 2000
    assert budgets.memories == 4000
    assert budgets.nexus_facts == 8000


def test_context_budgets_reject_out_of_bounds():
    with pytest.raises(ValidationError):
        ContextBudgets(total=50)
    with pytest.raises(ValidationError):
        ContextBudgets(memories=200_000)


def test_snapshot_has_commitments_due_in_slice_3():
    # Slice 3 legitimately adds commitments_due (the Slice 2 lock above is
    # replaced): open commitments due within 24h, rendered deterministically
    # as the "📌 Lembretes" block. Defaults to empty; covered by the digest.
    assert "commitments_due" in ContextSnapshot.model_fields
    snapshot = ContextSnapshot(
        task_id="t",
        session_id="s",
        budget_chars=12000,
        digest="0" * 64,
    )
    assert snapshot.commitments_due == []
    snapshot2 = ContextSnapshot(
        task_id="t",
        session_id="s",
        budget_chars=12000,
        digest="0" * 64,
        commitments_due=[
            CommitmentCtx(id="c1", title="pagar a conta", overdue=True),
        ],
    )
    assert snapshot2.commitments_due[0].overdue is True
    payload = snapshot_content_payload(snapshot2)
    assert payload["commitments_due"][0]["title"] == "pagar a conta"
    assert "id" not in payload["commitments_due"][0]  # random ids excluded
    # Extra fields are still forbidden.
    with pytest.raises(ValidationError):
        ContextSnapshot(
            task_id="t",
            session_id="s",
            budget_chars=12000,
            digest="0" * 64,
            commitments_due="not-a-list",  # type: ignore[arg-type]
        )


def test_snapshot_digest_is_deterministic_and_content_based():
    def make_snapshot():
        return ContextSnapshot(
            task_id="t",
            session_id="s",
            budget_chars=12000,
            conversation_tail=[_message_ctx()],
            preferences=[_memory_ctx(id="p1", kind=MemoryKind.PREFERENCE)],
            memories=[_memory_ctx(id="m2")],
            nexus_facts=[CanonicalFact(id="FACT_VOLTAGE_V", label="tensao", value=220.0, unit="V")],
            applied_chars={"memories": 10},
            truncated_sections=[],
            digest="0" * 64,
        )

    s1 = make_snapshot()
    s2 = make_snapshot()
    d1 = compute_snapshot_digest(snapshot_content_payload(s1))
    d2 = compute_snapshot_digest(snapshot_content_payload(s2))
    assert d1 == d2
    assert len(d1) == 64 and all(c in "0123456789abcdef" for c in d1)

    # Different ids / timestamps alone must NOT change the digest.
    s3 = make_snapshot().model_copy(
        update={
            "snapshot_id": "other",
            "conversation_tail": [_message_ctx(id="other-id")],
        }
    )
    assert compute_snapshot_digest(snapshot_content_payload(s3)) == d1

    # Different content MUST change the digest.
    s4 = make_snapshot().model_copy(
        update={"memories": [_memory_ctx(id="m2", title="outro titulo")]}
    )
    assert compute_snapshot_digest(snapshot_content_payload(s4)) != d1


def test_sections_require_label_and_lines_tuple():
    section = ContextSection(
        kind=ContextSectionKind.MEMORIES,
        label=LABEL_MEMORIES,
        lines=("a — b",),
    )
    assert section.lines == ("a — b",)
    with pytest.raises(ValidationError):
        ContextSection(kind=ContextSectionKind.MEMORIES, label="", lines=())
