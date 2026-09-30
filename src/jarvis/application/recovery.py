"""Startup recovery for tasks left non-terminal by a crashed process.

A task that is still non-terminal at startup can never resume: the in-flight
work (HTTP calls, LLM waits, user-facing coroutines) died with the old
process. Leaving it non-terminal would violate the terminal-state invariant
(COMPLETED / FAILED / CANCELLED are final) and would let monitoring believe
work is still in flight.

Recovery marks every such task FAILED with ``current_step="process_interrupted"``
(via the pure ``mark_interrupted`` transition) and appends an audited
``task.failed`` event (actor=system, outcome=failure). The update is
optimistic-concurrency guarded: if another worker transitioned the task first,
the conflict is skipped instead of double-marking it.
"""

from __future__ import annotations

from ..domain.contracts.audit import (
    AuditActor,
    AuditEventType,
    AuditLog,
    AuditOutcome,
)
from ..domain.errors import ErrorCode
from ..domain.state_machine import InvalidTransitionError, mark_interrupted
from ..observability.logging import get_logger
from ..ports.audit import AuditLogPort
from ..ports.repositories import TaskConflictError, TaskRepository

log = get_logger(__name__)


async def recover_interrupted_tasks(
    tasks: TaskRepository,
    audit: AuditLogPort,
    *,
    correlation_id: str,
) -> int:
    """Mark all non-terminal tasks FAILED. Returns the number recovered."""
    recovered = 0
    for task in await tasks.list_non_terminal():
        expected_version = task.version
        try:
            interrupted = mark_interrupted(task)
        except InvalidTransitionError:
            # Became terminal concurrently between the scan and here.
            continue
        try:
            await tasks.update(interrupted, expected_version=expected_version)
        except TaskConflictError:
            # Another worker won the race; do not audit twice.
            log.warning("recovery: task %s changed concurrently, skipping", task.id)
            continue
        await audit.append(
            AuditLog(
                correlation_id=correlation_id,
                session_id=task.session_id,
                task_id=task.id,
                event_type=AuditEventType.TASK_FAILED,
                actor=AuditActor.SYSTEM,
                outcome=AuditOutcome.FAILURE,
                error_code=ErrorCode.INTERNAL_ERROR,
                result_summary="process_interrupted: task was non-terminal at startup",
            )
        )
        recovered += 1
    return recovered
