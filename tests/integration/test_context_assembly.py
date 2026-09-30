"""Integration tests: context assembly inside the orchestrator pipeline.

Covers the Slice 2 wiring: the audited context.assembled event (digest,
no raw content), the "Você me disse:" rendering of standard user-stated
memories, the sensitive-memory exclusion from the provider-bound request,
and the fail-closed budget path that skips the LLM.
"""

from __future__ import annotations

from jarvis.domain.contracts.audit import AuditEventType
from jarvis.domain.contracts.memory import MemoryKind, Sensitivity
from jarvis.domain.contracts.task import ResponseSource
from jarvis.domain.errors import ErrorCode
from tests.conftest import build_orchestrator, make_nexus_handler, new_session_id

# Every token of the NEXUS question below must appear in the memory text:
# FTS5 uses AND semantics, so the memory only matches when it covers the
# whole query (including the "jarvis" vocative).
MEMORY_TEXT = "jarvis, como está o nexus na minha casa: meu equipamento é o Quadro Geral"
NEXUS_QUESTION = "JARVIS, como está o NEXUS?"


async def test_context_assembled_audited_with_digest_and_no_raw_content(orchestrator, repos):
    handler = make_nexus_handler(simulation={"running": True, "mode": "fault_injection"})
    orch = build_orchestrator(orchestrator.settings, repos, orchestrator.policy, handler)
    sid = await new_session_id(repos["sessions"])

    write_resp = await orch.handle_message(sid, f"Lembre-se: {MEMORY_TEXT}")
    assert "Guardei na memória" in write_resp.message

    resp = await orch.handle_message(sid, NEXUS_QUESTION)
    assert resp.source is ResponseSource.LLM_PLAN

    events = await repos["audit"].list_by_task(resp.task_id)
    types = [e.event_type for e in events]
    assert types == [
        AuditEventType.TASK_CREATED,
        AuditEventType.TASK_PLANNED,
        AuditEventType.POLICY_DECIDED,
        AuditEventType.TOOL_STARTED,
        AuditEventType.TOOL_COMPLETED,
        AuditEventType.VERIFICATION_COMPLETED,
        AuditEventType.CONTEXT_ASSEMBLED,
        AuditEventType.LLM_COMPLETED,
        AuditEventType.TASK_COMPLETED,
    ]
    assembled = next(e for e in events if e.event_type is AuditEventType.CONTEXT_ASSEMBLED)
    assert assembled.result_digest and len(assembled.result_digest) == 64
    blob = (assembled.request_summary or "") + (assembled.result_summary or "")
    assert "Quadro Geral" not in blob
    assert "como está o nexus" not in blob

    # The standard, user-stated memory renders as a "Você me disse:" note.
    assert "Você me disse:" in resp.message
    assert "Quadro Geral" in resp.message


async def test_sensitive_memory_never_reaches_provider_or_response(
    orchestrator, repos, memory_service
):
    handler = make_nexus_handler()
    orch = build_orchestrator(orchestrator.settings, repos, orchestrator.policy, handler)
    sid = await new_session_id(repos["sessions"])

    # Both memories match the retrieval query; only the standard one may
    # surface. The standard control memory proves the test is not vacuous.
    await memory_service.create(
        kind=MemoryKind.FACT,
        title="dado sensivel",
        content="jarvis, como está o nexus: meu diagnostico medico particular",
        sensitivity=Sensitivity.SENSITIVE,
        correlation_id="test-sensitive",
    )
    await memory_service.create(
        kind=MemoryKind.FACT,
        title="dado publico",
        content="jarvis, como está o nexus: meu equipamento é o Quadro Geral",
        sensitivity=Sensitivity.STANDARD,
        correlation_id="test-standard",
    )

    resp = await orch.handle_message(sid, NEXUS_QUESTION)
    assert resp.source is ResponseSource.LLM_PLAN

    # Control: the standard memory renders, so retrieval worked.
    assert "Você me disse:" in resp.message
    assert "Quadro Geral" in resp.message

    # The provider-bound object carries no sensitive content.
    assert len(orch.llm.requests) == 1
    request = orch.llm.requests[0]
    provider_blob = "\n".join(
        line for section in request.context_sections for line in section.lines
    )
    assert "diagnostico" not in provider_blob
    assert "medico" not in provider_blob

    # ...and the rendered response does not leak it either.
    assert "diagnostico" not in resp.message
    assert "medico" not in resp.message


async def test_facts_over_budget_skips_llm_fail_closed(orchestrator, repos):
    settings = orchestrator.settings.model_copy(update={"ctx_facts_chars": 100})
    handler = make_nexus_handler()
    orch = build_orchestrator(settings, repos, orchestrator.policy, handler)
    sid = await new_session_id(repos["sessions"])

    resp = await orch.handle_message(sid, NEXUS_QUESTION)

    # Fail-closed: the LLM stage is skipped, the deterministic renderer
    # answers from verified facts.
    assert resp.source is ResponseSource.DETERMINISTIC_FALLBACK
    assert orch.llm.requests == []
    assert "NEXUS" in resp.message

    events = await repos["audit"].list_by_task(resp.task_id)
    types = [e.event_type for e in events]
    assert AuditEventType.CONTEXT_ASSEMBLED in types
    fallback = next(e for e in events if e.event_type is AuditEventType.LLM_FALLBACK)
    assert fallback.error_code is ErrorCode.CONTEXT_BUDGET_EXCEEDED
