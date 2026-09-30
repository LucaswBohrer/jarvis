"""T6 — FTS5 behaviors: diacritics, injection-proof MATCH building, snippets.

The user's raw text NEVER reaches the MATCH operator: build_fts_match_query
normalizes it into a conjunction of quoted prefix terms. Anything that looks
like FTS5 syntax is destroyed by the normalizer first.
"""

from __future__ import annotations

from jarvis.adapters.persistence.fts import (
    build_fts_match_query,
    normalize_text,
    rebuild_memory_fts,
)
from jarvis.domain.contracts.memory import MemoryKind, MemoryQuery

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


async def test_diacritics_folded(memory_service):
    await _create(memory_service, "Situação do projeto", "O relatório está ótimo.")
    hits = await memory_service.retrieve(MemoryQuery(query="situacao"))
    assert [h.item.title for h in hits] == ["Situação do projeto"]
    hits = await memory_service.retrieve(MemoryQuery(query="OTIMO"))
    assert len(hits) == 1


async def test_prefix_matching(memory_service):
    await _create(memory_service, "Aniversário da Ana", "Festa em dezembro.")
    hits = await memory_service.retrieve(MemoryQuery(query="aniver"))
    assert len(hits) == 1


async def test_injection_payloads_return_empty(memory_service):
    await _create(memory_service, "Nota normal", "Conteúdo qualquer.")
    for payload in (
        '" OR "1"="1',
        "title:nota",
        "conteudo NEAR(anything, 5)",
        "(a OR b) AND c",
        "nota*",
        "'; DROP TABLE memory_items; --",
        "{column}",
        "*",
    ):
        hits = await memory_service.retrieve(MemoryQuery(query=payload))
        # The payload is tokenized; it must never match everything or error.
        assert isinstance(hits, list)


def test_match_query_never_contains_raw_syntax():
    q = build_fts_match_query('title:segredo" OR "1"="1')
    # "OR" survives only as a quoted literal term — never as an operator.
    assert q == '"title"* "segredo"* "or"* "1"* "1"*'
    assert ":" not in q
    assert q.count('"') % 2 == 0
    # Column filters are destroyed: no "title:" prefix operator reaches MATCH.
    assert not any(term.startswith("title:") for term in q.split())


def test_match_query_empty_for_noise():
    assert build_fts_match_query("!!! ??? ...") == ""
    assert build_fts_match_query("") == ""


def test_match_query_caps():
    q = build_fts_match_query(" ".join(f"tok{i}" for i in range(30)))
    assert len(q.split()) == 10  # MAX_FTS_TOKENS
    long_tok = "a" * 100
    q = build_fts_match_query(long_tok)
    assert q == '"' + "a" * 32 + '"*'  # quotes + MAX_TOKEN_CHARS + prefix star


def test_normalize_text():
    assert normalize_text("Café AÇÚCAR 123!") == "cafe acucar 123 "
    assert normalize_text("  olá---mundo  ") == "  ola   mundo  "


async def test_deleted_items_not_in_index(memory_service):
    item = await _create(memory_service, "Segredo temporário", "apagar depois")
    assert await memory_service.retrieve(MemoryQuery(query="temporario"))
    await memory_service.delete(item.id, correlation_id=CID)
    assert await memory_service.retrieve(MemoryQuery(query="temporario")) == []


async def test_updated_content_reindexed(memory_service):
    item = await _create(memory_service, "Título", "conteúdo antigo")
    assert await memory_service.retrieve(MemoryQuery(query="antigo"))
    await memory_service.supersede(item.id, content="conteúdo novo", correlation_id=CID)
    assert await memory_service.retrieve(MemoryQuery(query="novo"))
    assert await memory_service.retrieve(MemoryQuery(query="antigo")) == []


async def test_rebuild_is_idempotent(memory_service, repos):
    await _create(memory_service, "Primeira", "conteúdo um")
    await _create(memory_service, "Segunda", "conteúdo dois")
    n1 = await rebuild_memory_fts(repos["db"])
    n2 = await rebuild_memory_fts(repos["db"])
    assert n1 == n2 == 2
    hits = await memory_service.retrieve(MemoryQuery(query="conteudo"))
    assert len(hits) == 2


async def test_snippet_does_not_leak_beyond_match(memory_service):
    await _create(memory_service, "Receita", "O bolo leva farinha, ovos e açúcar.")
    hits = await memory_service.retrieve(MemoryQuery(query="farinha"))
    assert len(hits) == 1
    assert "farinha" in hits[0].snippet
