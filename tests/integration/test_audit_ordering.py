"""F3.4.1 regression: audit event order is causal, not clock-dependent.

``occurred_at`` comes from the OS clock. On Windows its granularity is
~15.6 ms, so several causally-ordered events routinely share one timestamp;
the old ``id ASC`` tiebreak (random UUID) then produced arbitrary orders and
three tests failed there. ``list_by_task`` now tie-breaks on SQLite ``rowid``
(true insertion order, no migration), so the observed order always matches
the append (causal) order — even when every timestamp is identical.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jarvis.domain.contracts.audit import (
    AuditActor,
    AuditEventType,
    AuditLog,
    AuditOutcome,
)
from jarvis.domain.contracts.task import Intent, TaskInput, TaskKind, TaskRecord
from tests.conftest import new_session_id

FROZEN = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)  # one Windows clock tick


async def _task(repos: dict[str, Any]) -> TaskRecord:
    sid = await new_session_id(repos["sessions"])
    task = TaskRecord(
        session_id=sid,
        kind=TaskKind.NEXUS_STATUS,
        input=TaskInput(message_id="m", utterance="u", intent=Intent.NEXUS_STATUS),
    )
    await repos["tasks"].create(task)
    return task


async def _append(repos: dict[str, Any], task: TaskRecord, *event_types: AuditEventType) -> None:
    for event_type in event_types:
        await repos["audit"].append(
            AuditLog(
                correlation_id="c",
                session_id=task.session_id,
                task_id=task.id,
                event_type=event_type,
                actor=AuditActor.SYSTEM,
                outcome=AuditOutcome.SUCCESS,
                occurred_at=FROZEN,  # identical timestamps: the Windows condition
            )
        )


async def test_identical_timestamps_preserve_append_order(repos: dict[str, Any]) -> None:
    """Same-tick events come back in the order they were appended."""
    task = await _task(repos)
    order = (
        AuditEventType.TASK_CREATED,
        AuditEventType.TASK_PLANNED,
        AuditEventType.POLICY_DECIDED,
        AuditEventType.MEMORY_CREATED,
    )
    await _append(repos, task, *order)
    got = [e.event_type for e in await repos["audit"].list_by_task(task.id)]
    assert got == list(order)


async def test_task_completed_comes_after_llm_completed_on_tie(
    repos: dict[str, Any],
) -> None:
    """The exact pair that failed on Windows (e2e critical slice)."""
    task = await _task(repos)
    order = (
        AuditEventType.TASK_CREATED,
        AuditEventType.LLM_COMPLETED,
        AuditEventType.TASK_COMPLETED,
    )
    await _append(repos, task, *order)
    got = [e.event_type for e in await repos["audit"].list_by_task(task.id)]
    assert got == list(order)
    assert got.index(AuditEventType.LLM_COMPLETED) < got.index(AuditEventType.TASK_COMPLETED)


async def test_policy_decided_precedes_memory_created_on_tie(
    repos: dict[str, Any],
) -> None:
    """The exact pair that failed on Windows (memory audit)."""
    task = await _task(repos)
    await _append(repos, task, AuditEventType.POLICY_DECIDED, AuditEventType.MEMORY_CREATED)
    got = [e.event_type for e in await repos["audit"].list_by_task(task.id)]
    assert got == [AuditEventType.POLICY_DECIDED, AuditEventType.MEMORY_CREATED]
