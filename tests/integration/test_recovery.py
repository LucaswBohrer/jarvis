"""Startup crash recovery (§22).

A process crash must never leave a task non-terminal: on startup every
leftover task is marked FAILED (current_step=process_interrupted) with an
audited task.failed event, and terminal states stay immutable.
"""

from __future__ import annotations

import pytest

from jarvis.api.main import create_app, lifespan
from jarvis.application.recovery import recover_interrupted_tasks
from jarvis.config import Settings
from jarvis.domain.contracts.audit import AuditEventType
from jarvis.domain.contracts.common import new_id
from jarvis.domain.contracts.session import SessionRecord
from jarvis.domain.contracts.task import (
    TaskInput,
    TaskKind,
    TaskRecord,
    TaskState,
)
from jarvis.domain.errors import ErrorCode
from jarvis.domain.state_machine import InvalidTransitionError, transition


def _make_task(session_id: str, state: TaskState = TaskState.PENDING, **kw) -> TaskRecord:
    return TaskRecord(
        session_id=session_id,
        kind=TaskKind.NEXUS_STATUS,
        input=TaskInput(message_id=new_id(), utterance="como está o nexus?"),
        state=state,
        **kw,
    )


async def test_recover_marks_non_terminal_failed_and_audits(repos):
    session = SessionRecord()
    await repos["sessions"].create(session)
    pending = await repos["tasks"].create(_make_task(session.id, TaskState.PENDING))
    running = await repos["tasks"].create(_make_task(session.id, TaskState.RUNNING))

    recovered = await recover_interrupted_tasks(
        repos["tasks"], repos["audit"], correlation_id="recovery-test"
    )
    assert recovered == 2

    for original in (pending, running):
        done = await repos["tasks"].get(original.id)
        assert done.state is TaskState.FAILED
        assert done.current_step == "process_interrupted"
        assert done.error_code is ErrorCode.INTERNAL_ERROR
        events = await repos["audit"].list_by_task(original.id)
        assert len(events) == 1
        assert events[0].event_type is AuditEventType.TASK_FAILED
        assert "process_interrupted" in (events[0].result_summary or "")
        # Terminal state is final.
        with pytest.raises(InvalidTransitionError):
            transition(done, TaskState.RUNNING)


async def test_recover_skips_terminal_tasks(repos):
    session = SessionRecord()
    await repos["sessions"].create(session)
    cancelled = await repos["tasks"].create(
        _make_task(session.id, TaskState.CANCELLED, error_code=ErrorCode.TASK_CANCELLED)
    )

    recovered = await recover_interrupted_tasks(
        repos["tasks"], repos["audit"], correlation_id="recovery-test"
    )
    assert recovered == 0
    untouched = await repos["tasks"].get(cancelled.id)
    assert untouched.state is TaskState.CANCELLED
    assert await repos["audit"].list_by_task(cancelled.id) == []


async def test_lifespan_runs_recovery_on_startup(db_url, stub_nexus):
    """End-to-end: a task stranded by a crash is FAILED by app startup."""
    settings = Settings(
        database_url=db_url,
        nexus_base_url=stub_nexus.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )
    app = create_app(settings)
    state = app.state.jarvis
    session = SessionRecord()
    await state.sessions.create(session)
    task = await state.tasks.create(_make_task(session.id, TaskState.PLANNING))

    async with lifespan(app):
        pass  # startup ran recovery; shutdown released resources

    done = await state.tasks.get(task.id)
    assert done.state is TaskState.FAILED
    assert done.current_step == "process_interrupted"
    events = await state.audit.list_by_task(task.id)
    assert [e.event_type for e in events] == [AuditEventType.TASK_FAILED]
