"""Explicit task state machine.

Seven states, guarded transitions, terminal immutability.
Optimistic concurrency: every transition bumps ``version``; the repository
persists with ``WHERE id=? AND version=?`` and reports a conflict when no row
is updated, so a task can never execute twice.
"""

from __future__ import annotations

from .contracts.common import utcnow
from .contracts.task import TERMINAL_STATES, TaskRecord, TaskState
from .errors import ErrorCode

_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.PLANNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.PLANNING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.WAITING_CONFIRMATION,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.WAITING_CONFIRMATION: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.RUNNING: frozenset({TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def allowed_transitions(state: TaskState) -> frozenset[TaskState]:
    """Destinations permitted from ``state`` (empty for terminal states)."""
    return _ALLOWED[state]


def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
    return to_state in _ALLOWED[from_state]


class InvalidTransitionError(Exception):
    def __init__(self, from_state: TaskState, to_state: TaskState) -> None:
        super().__init__(f"invalid task transition {from_state.value} -> {to_state.value}")
        self.from_state = from_state
        self.to_state = to_state


def transition(
    record: TaskRecord,
    to_state: TaskState,
    *,
    current_step: str | None = None,
) -> TaskRecord:
    """Pure transition: validates the move and guards, returns an updated copy.

    Guards:
      - terminal states are immutable (no outgoing transition);
      - PLANNING -> RUNNING requires a persisted plan;
      - any -> COMPLETED requires a result;
      - any -> FAILED requires an error_code.
    The caller sets plan/result/error_code on the record before transitioning.
    """
    if not can_transition(record.state, to_state):
        raise InvalidTransitionError(record.state, to_state)
    if to_state is TaskState.RUNNING and record.plan is None:
        raise InvalidTransitionError(record.state, to_state)
    if to_state is TaskState.COMPLETED and record.result is None:
        raise InvalidTransitionError(record.state, to_state)
    if to_state is TaskState.FAILED and record.error_code is None:
        raise InvalidTransitionError(record.state, to_state)
    if record.state in TERMINAL_STATES:
        raise InvalidTransitionError(record.state, to_state)

    return record.model_copy(
        update={
            "state": to_state,
            "current_step": current_step if current_step is not None else record.current_step,
            "version": record.version + 1,
            "updated_at": utcnow(),
        }
    )


def mark_interrupted(record: TaskRecord) -> TaskRecord:
    """Recovery path: a non-terminal task found after a process crash."""
    if record.state in TERMINAL_STATES:
        return record
    interrupted = record.model_copy(update={"error_code": ErrorCode.INTERNAL_ERROR})
    return transition(interrupted, TaskState.FAILED, current_step="process_interrupted")
