"""Security: secret-scan canary for the audit log.

A canary payload containing secret-shaped values flows through a full
message turn; the test then scans every persisted audit entry and rejects
the run if any canary fragment (or generic secret pattern) appears.
"""

from __future__ import annotations

import re

from jarvis.domain.contracts.audit import AuditEventType, AuditLog, AuditOutcome
from jarvis.domain.contracts.common import new_id

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*\S+"),
]


async def test_audit_never_persists_secret_shaped_values(repos):
    """Direct audit-path canary: redacted summary must not carry the secret."""
    from jarvis.domain.contracts.session import SessionRecord
    from jarvis.domain.contracts.task import TaskInput, TaskKind, TaskRecord
    from jarvis.security.redaction import redact

    session = SessionRecord()
    await repos["sessions"].create(session)
    task = TaskRecord(
        session_id=session.id,
        kind=TaskKind.NEXUS_STATUS,
        input=TaskInput(message_id=new_id(), utterance="como está o nexus?"),
    )
    await repos["tasks"].create(task)

    canary = "sk-canary-9f8e7d6c5b4a"
    raw = {"api_key": canary, "voltage": 220.0}
    cleaned = redact(raw)
    entry = AuditLog(
        id=new_id(),
        correlation_id=new_id(),
        task_id=task.id,
        session_id=session.id,
        event_type=AuditEventType.TOOL_COMPLETED,
        outcome=AuditOutcome.SUCCESS,
        request_summary=str(cleaned),
        result_summary="ok",
    )
    await repos["audit"].append(entry)
    entries = await repos["audit"].list_by_task(entry.task_id)
    for e in entries:
        blob = (e.request_summary or "") + (e.result_summary or "")
        assert "sk-canary" not in blob
        for pat in SECRET_PATTERNS:
            assert not pat.search(blob), f"secret pattern hit: {pat.pattern}"


async def test_full_turn_audit_has_no_raw_nexus_payload(repos, settings, policy):
    """Full pipeline canary: no raw NEXUS field names/values in audit summaries."""
    from tests.conftest import build_orchestrator, make_nexus_handler, new_session_id

    handler = make_nexus_handler()
    orch = build_orchestrator(settings, repos, policy, handler)
    sid = await new_session_id(repos["sessions"])
    resp = await orch.handle_message(sid, "como está o nexus?")
    for e in await repos["audit"].list_by_task(resp.task_id):
        blob = (e.request_summary or "") + (e.result_summary or "")
        for fragment in ("power_factor", "active_power", "1980", "0.9", "temperature"):
            assert fragment not in blob, f"raw payload fragment in audit: {fragment}"
        for pat in SECRET_PATTERNS:
            assert not pat.search(blob), f"secret pattern hit: {pat.pattern}"
