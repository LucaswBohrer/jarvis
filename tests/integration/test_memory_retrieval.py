"""T7 — Retrieval semantics: what the read path may and may not return.

Guarantees: only active items by default (pending included on request),
superseded/deleted/revoked/expired never returned, llm_inferred provenance
never returned, expired valid_until filtered, kind filter honored.
"""

from __future__ import annotations

from datetime import timedelta

from jarvis.domain.contracts.common import utcnow
from jarvis.domain.contracts.memory import (
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryStatus,
    Provenance,
)

CID = "test-correlation"


async def _create(svc, title, content, **over):
    args = {
        "kind": MemoryKind.FACT,
        "title": title,
        "content": content,
        "correlation_id": CID,
    }
    args.update(over)
    return await svc.create(**args)


async def test_retrieve_returns_active_only(memory_service):
    item = await _create(memory_service, "Café forte", "prefiro café forte")
    hits = await memory_service.retrieve(MemoryQuery(query="cafe"))
    assert [h.item.id for h in hits] == [item.id]
    assert all(h.item.status is MemoryStatus.ACTIVE for h in hits)


async def test_superseded_old_version_excluded(memory_service):
    old = await _create(memory_service, "Time", "torço para o time azul")
    await memory_service.supersede(old.id, content="torço para o time vermelho", correlation_id=CID)
    hits = await memory_service.retrieve(MemoryQuery(query="time"))
    assert len(hits) == 1
    assert hits[0].item.content == "torço para o time vermelho"


async def test_deleted_revoked_excluded(memory_service):
    doomed = await _create(memory_service, "Apagar", "item a ser apagado")
    gone = await _create(memory_service, "Revogar", "item a ser revogado")
    await memory_service.delete(doomed.id, correlation_id=CID)
    await memory_service.revoke(gone.id, correlation_id=CID)
    assert await memory_service.retrieve(MemoryQuery(query="apagar")) == []
    assert await memory_service.retrieve(MemoryQuery(query="revogar")) == []


async def test_expired_valid_until_excluded(memory_service, repos):
    # The service refuses to create an already-expired item, so insert
    # directly to exercise the retrieval-side filter.
    expired = MemoryItem(
        kind=MemoryKind.FACT,
        title="Promoção",
        content="promoção relâmpago",
        provenance=Provenance.USER_EXPLICIT,
        confidence=1.0,
        status=MemoryStatus.ACTIVE,
        valid_until=utcnow() - timedelta(seconds=1),
    )
    await repos["memory"].create(expired)
    assert await memory_service.retrieve(MemoryQuery(query="promocao")) == []


async def test_future_valid_until_included(memory_service):
    await _create(
        memory_service,
        "Lembrete",
        "reunião importante",
        valid_until=utcnow() + timedelta(days=1),
    )
    hits = await memory_service.retrieve(MemoryQuery(query="reuniao"))
    assert len(hits) == 1


async def test_pending_excluded_by_default(memory_service, repos):
    pending = MemoryItem(
        kind=MemoryKind.FACT,
        title="Rascunho",
        content="rascunho pendente",
        provenance=Provenance.USER_EXPLICIT,
        confidence=1.0,
        status=MemoryStatus.PENDING,
    )
    await repos["memory"].create(pending)
    assert await memory_service.retrieve(MemoryQuery(query="rascunho")) == []
    hits = await memory_service.retrieve(MemoryQuery(query="rascunho", include_pending=True))
    assert [h.item.id for h in hits] == [pending.id]


async def test_llm_inferred_never_returned(memory_service, repos):
    inferred = MemoryItem(
        kind=MemoryKind.FACT,
        title="Palpite",
        content="palpite do modelo",
        provenance=Provenance.LLM_INFERRED,
        confidence=0.5,
        status=MemoryStatus.ACTIVE,
    )
    await repos["memory"].create(inferred)
    assert await memory_service.retrieve(MemoryQuery(query="palpite")) == []
    # Even with include_pending=True, llm_inferred stays out.
    assert (await memory_service.retrieve(MemoryQuery(query="palpite", include_pending=True))) == []


async def test_kind_filter(memory_service):
    await _create(memory_service, "Cor favorita", "azul", kind=MemoryKind.PREFERENCE)
    await _create(memory_service, "Cor do céu", "azul também", kind=MemoryKind.FACT)
    hits = await memory_service.retrieve(
        MemoryQuery(query="azul", kinds=frozenset({MemoryKind.PREFERENCE}))
    )
    assert len(hits) == 1
    assert hits[0].item.kind is MemoryKind.PREFERENCE


async def test_limit_respected(memory_service):
    for i in range(5):
        await _create(memory_service, f"Nota {i}", "conteúdo repetido xyz")
    hits = await memory_service.retrieve(MemoryQuery(query="repetido", limit=2))
    assert len(hits) == 2


async def test_empty_tokens_return_empty(memory_service):
    await _create(memory_service, "Qualquer", "coisa")
    assert await memory_service.retrieve(MemoryQuery(query="!!!")) == []
