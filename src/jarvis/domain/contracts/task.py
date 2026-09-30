"""Task contracts: input, fixed plan, record, result, assistant response."""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field, model_validator

from ..errors import ErrorCode
from .common import SCHEMA_VERSION, FrozenModel, new_id, utcnow


class TaskKind(str, Enum):
    NEXUS_STATUS = "nexus_status"
    MEMORY_WRITE = "memory_write"
    MEMORY_READ = "memory_read"
    COMMITMENT_CREATE = "commitment_create"
    COMMITMENT_FULFILL = "commitment_fulfill"


class TaskState(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})


class Intent(str, Enum):
    NEXUS_STATUS = "NEXUS_STATUS"
    MEMORY_WRITE = "MEMORY_WRITE"
    MEMORY_READ = "MEMORY_READ"
    COMMITMENT_CREATE = "COMMITMENT_CREATE"
    COMMITMENT_FULFILL = "COMMITMENT_FULFILL"


class TaskOutcome(str, Enum):
    """Semantic outcome of a finished task.

    UNAVAILABLE is not a failure: the task "tell me the NEXUS status" was
    processed successfully and the honest answer is "NEXUS is unreachable".
    FAILED is reserved for internal failures with no safe response.
    """

    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ResponseStatus(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    STALE = "stale"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    SIMULATION = "simulation"


class TaskInput(FrozenModel):
    message_id: str = Field(min_length=1, max_length=64)
    utterance: str = Field(min_length=1, max_length=2000)
    # None = intent not recognized; the task fails without executing.
    intent: Intent | None = None


class TaskPlan(FrozenModel):
    """Fixed execution plan for the intent. No free-form steps."""

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    task_id: str = Field(min_length=1, max_length=64)
    steps: list[str] = Field(min_length=1, max_length=16)
    capability_set: list[str] = Field(min_length=1, max_length=8)
    deadline_at: AwareDatetime


class TaskResult(FrozenModel):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    outcome: TaskOutcome
    response_status: ResponseStatus | None = None
    assistant_message_id: str | None = Field(default=None, max_length=64)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def _outcome_consistency(self) -> TaskResult:
        if self.outcome is TaskOutcome.ERROR and self.error_code is None:
            raise ValueError("ERROR outcome requires error_code")
        if self.outcome is not TaskOutcome.ERROR and self.error_code is not None:
            raise ValueError("error_code only valid with ERROR outcome")
        return self


class TaskRecord(FrozenModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    kind: TaskKind
    state: TaskState = TaskState.PENDING
    input: TaskInput
    plan: TaskPlan | None = None
    result: TaskResult | None = None
    error_code: ErrorCode | None = None
    current_step: str | None = Field(default=None, max_length=128)
    version: int = Field(default=1, ge=1)  # optimistic concurrency
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _terminal_has_result(self) -> TaskRecord:
        if self.state in TERMINAL_STATES and self.result is None and self.error_code is None:
            # CANCELLED carries TASK_CANCELLED via error_code OR result; be strict.
            raise ValueError("terminal task requires result or error_code")
        return self


class ResponseSource(str, Enum):
    LLM_PLAN = "llm_plan"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class AssistantResponse(FrozenModel):
    """Final user-facing response. No raw payloads, no internal details."""

    task_id: str = Field(min_length=1, max_length=64)
    status: ResponseStatus
    message: str = Field(min_length=1, max_length=4000)
    source: ResponseSource
    fact_ids: list[str] = Field(default_factory=list, max_length=64)
    observed_at: AwareDatetime = Field(default_factory=utcnow)
