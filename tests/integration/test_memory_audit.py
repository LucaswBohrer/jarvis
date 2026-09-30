"""T14 — Audit: every memory op leaves a complete, redacted trail.

Guarantees: policy.decided precedes every write and read; each lifecycle op
audits its own event; blocked writes audit memory.write_blocked; the audit
trail never carries raw secret values or raw memory content in request
summaries.
"""

from __future__ import annotations

import pytest

from jarvis.application.memory_service import MemoryService
from jarvis.domain.contracts.audit import AuditEventType
from jarvis.domain.contracts.memory import MemoryKind, MemoryQuery
from jarvis.domain.contracts.task import Intent, TaskInput, TaskKind, TaskRecord
from jarvis.domain.errors import ErrorCode, JarvisException
from jarvis.ports.clock import SystemClock
from jarvis.security.policy import PolicyEngine
from tests.conftest import new_session_id

CID = "test-correlation"


async def _task_id(repos) -> str:
    """A real task row, so audit FKs hold."""
    session_id = await new_session_id(repos["sessions"])
    task = TaskRecord(
        session_id=session_id,
        kind=TaskKind.MEMORY_WRITE,
        input=TaskInput(message_id="m1", utterance="test", intent=Intent.MEMORY_WRITE),
    )
    await repos["tasks"].create(task)
    return task.id


async def _all_events(repos, task_id):
    return await repos["audit"].list_by_task(task_id, limit=500)


async def test_create_audits_policy_then_created(memory_service, repos):
    tid = await _task_id(repos)
    item = await memory_service.create(
        kind=MemoryKind.FACT,
        title="Café",
        content="gosto de café",
        task_id=tid,
        correlation_id=CID,
    )
    assert item.id is not None
    events = [e.event_type for e in await _all_events(repos, tid)]
    assert AuditEventType.POLICY_DECIDED in events
    assert AuditEventType.MEMORY_CREATED in events
    # Policy decision comes first.
    assert events.index(AuditEventType.POLICY_DECIDED) < events.index(AuditEventType.MEMORY_CREATED)


async def test_lifecycle_events(memory_service, repos):
    tid = await _task_id(repos)
    item = await memory_service.create(
        kind=MemoryKind.FACT, title="t", content="c", task_id=tid, correlation_id=CID
    )
    new = await memory_service.supersede(item.id, content="c2", task_id=tid, correlation_id=CID)
    # The superseded predecessor is terminal: immutable by design.
    with pytest.raises(JarvisException) as exc:
        await memory_service.delete(item.id, task_id=tid, correlation_id=CID)
    assert exc.value.error.code is ErrorCode.INPUT_INVALID
    # Revoked items are terminal too: physical erasure flows delete -> purge.
    with pytest.raises(JarvisException) as exc:
        await memory_service.purge(new.id, task_id=tid, correlation_id=CID)
    assert exc.value.error.code is ErrorCode.INPUT_INVALID
    await memory_service.delete(new.id, task_id=tid, correlation_id=CID)
    await memory_service.purge(new.id, task_id=tid, correlation_id=CID)

    events = [e.event_type for e in await _all_events(repos, tid)]
    for expected in (
        AuditEventType.MEMORY_CREATED,
        AuditEventType.MEMORY_SUPERSEDED,
        AuditEventType.MEMORY_DELETED,
        AuditEventType.MEMORY_PURGED,
    ):
        assert expected in events, expected


async def test_blocked_write_audited_without_secret(memory_service, repos):
    tid = await _task_id(repos)
    secret = "sk-abcdefghijklmnopqrstuvwx"  # noqa: S105 - synthetic test vector, not a credential
    with pytest.raises(JarvisException) as exc:
        await memory_service.create(
            kind=MemoryKind.FACT,
            title="chave",
            content=f"a chave é {secret}",
            task_id=tid,
            correlation_id=CID,
        )
    assert exc.value.error.code is ErrorCode.MEMORY_SECRET_DETECTED
    events = await _all_events(repos, tid)
    blocked = [e for e in events if e.event_type is AuditEventType.MEMORY_WRITE_BLOCKED]
    assert blocked, "expected a memory.write_blocked audit event"
    # The raw secret appears NOWHERE in the audit trail.
    for e in events:
        blob = (e.request_summary or "") + (e.result_summary or "")
        assert secret not in blob


async def test_request_summaries_never_carry_raw_content(memory_service, repos):
    tid = await _task_id(repos)
    content = "detalhe confidencial 12345"
    await memory_service.create(
        kind=MemoryKind.FACT,
        title="Nota",
        content=content,
        task_id=tid,
        correlation_id=CID,
    )
    for e in await _all_events(repos, tid):
        blob = (e.request_summary or "") + (e.result_summary or "")
        assert content not in blob, e.event_type


async def test_policy_deny_leaves_durable_audit(memory_service, repos, tmp_path):
    """D31: a policy DENY is audited in its own transaction — the audit row
    survives even though the write never happens (previously the rollback
    erased it)."""
    deny_toml = tmp_path / "deny.toml"
    deny_toml.write_text(
        'policy_version = "policy.toml:2"\n'
        "[[rule]]\n"
        'id = "nexus-only"\n'
        'effect = "allow"\n'
        'capability = "nexus.status.read"\n'
        'tool = "nexus.status"\n'
        'resource = "configured-nexus"\n'
        'methods = ["GET"]\n'
        'path_prefix = "/api/v1/"\n'
        "loopback_only = true\n"
        "follow_redirects = false\n"
        "max_response_bytes = 262144\n"
        "deadline_ms = 3500\n"
    )
    denying = MemoryService(
        db=repos["db"],
        memory=repos["memory"],
        audit=repos["audit"],
        policy=PolicyEngine.from_toml(deny_toml),
        clock=SystemClock(),
    )
    tid = await _task_id(repos)
    with pytest.raises(JarvisException) as exc:
        await denying.create(
            kind=MemoryKind.FACT,
            title="t",
            content="c",
            task_id=tid,
            correlation_id=CID,
        )
    assert exc.value.error.code is ErrorCode.POLICY_DENIED
    events = await _all_events(repos, tid)
    denied = [
        e
        for e in events
        if e.event_type is AuditEventType.POLICY_DECIDED and e.outcome.value == "denied"
    ]
    assert denied, "expected a durable policy.decided/DENIED audit row"
    # And nothing was persisted.
    assert await repos["memory"].list_items() == []


async def test_read_audits_policy_decision(memory_service, repos):
    tid = await _task_id(repos)
    await memory_service.read(MemoryQuery(query="nada"), task_id=tid, correlation_id=CID)
    events = [e.event_type for e in await _all_events(repos, tid)]
    assert AuditEventType.POLICY_DECIDED in events


async def test_export_audited_once(memory_service, repos):
    tid = await _task_id(repos)
    await memory_service.create(kind=MemoryKind.FACT, title="t", content="c", correlation_id=CID)
    items = await memory_service.export(task_id=tid, correlation_id=CID)
    assert len(items) == 1
    events = [e.event_type for e in await _all_events(repos, tid)]
    assert events.count(AuditEventType.MEMORY_EXPORTED) == 1
    # One policy decision for the whole export, not one per item.
    assert events.count(AuditEventType.POLICY_DECIDED) == 1
