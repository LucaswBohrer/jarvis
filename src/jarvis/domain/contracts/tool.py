"""Tool contracts: typed request/result envelope around every external capability."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from ..errors import JarvisError
from .common import SCHEMA_VERSION, EvidenceRef, FrozenModel, new_id, utcnow
from .memory import (
    MemoryKind,
    MemoryQuery,
    MemoryReadResult,
    MemoryWriteOp,
    MemoryWriteResult,
    Sensitivity,
)
from .nexus import CanonicalNexusStatus, NexusStatusQuery


class ToolName(str, Enum):
    NEXUS_STATUS = "nexus.status"
    MEMORY_WRITE = "memory.write"
    MEMORY_READ = "memory.read"


class Capability(str, Enum):
    NEXUS_STATUS_READ = "nexus.status.read"
    MEMORY_WRITE = "memory.write"
    MEMORY_READ = "memory.read"


class ToolOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class MemoryWriteArgs(FrozenModel):
    """Arguments for the internal memory.write tool.

    The origin field is a singleton literal on purpose: no other value can be
    constructed, so a write without an explicit user command is a contract
    violation, not a policy decision. Policy additionally requires an exact
    origin match (require_origin), so a request carrying *other* arguments
    (e.g. NexusStatusQuery) is denied even when tool_name/memory.write pairs
    up — that is the "no origin => DENY" case.
    """

    tool_arg_kind: Literal["memory_write"] = "memory_write"
    op: MemoryWriteOp
    kind: MemoryKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    target_id: str | None = Field(default=None, min_length=1, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sensitivity: Sensitivity = Sensitivity.STANDARD
    valid_until: AwareDatetime | None = None
    origin: Literal["user_explicit_command"]  # required on purpose: no default

    @model_validator(mode="after")
    def _op_fields(self) -> MemoryWriteArgs:
        if self.op is MemoryWriteOp.CREATE:
            if self.kind is None:
                raise ValueError("create requires kind")
            if not self.title:
                raise ValueError("create requires title")
            if not self.content:
                raise ValueError("create requires content")
            if self.target_id is not None:
                raise ValueError("create must not carry target_id")
        elif self.op is MemoryWriteOp.SUPERSEDE:
            if not self.target_id:
                raise ValueError("supersede requires target_id")
            if not (self.title or self.content):
                raise ValueError("supersede requires a new title and/or content")
            if self.kind is not None:
                raise ValueError("supersede inherits kind from the target")
        else:
            if not self.target_id:
                raise ValueError(f"{self.op.value} requires target_id")
            if self.kind is not None or self.title is not None or self.content is not None:
                raise ValueError(f"{self.op.value} takes no kind/title/content")
            if self.confidence is not None or self.valid_until is not None:
                raise ValueError(f"{self.op.value} takes no confidence/valid_until")
        return self


class MemoryReadArgs(FrozenModel):
    tool_arg_kind: Literal["memory_read"] = "memory_read"
    query: MemoryQuery


ToolArguments = Annotated[
    NexusStatusQuery | MemoryWriteArgs | MemoryReadArgs,
    Field(discriminator="tool_arg_kind"),
]

#: The single valid (tool_name, arguments) pairing per tool. A mismatch is a
#: contract violation (T1), independent of the policy decision.
_TOOL_ARGUMENTS: dict[ToolName, type[NexusStatusQuery | MemoryWriteArgs | MemoryReadArgs]] = {
    ToolName.NEXUS_STATUS: NexusStatusQuery,
    ToolName.MEMORY_WRITE: MemoryWriteArgs,
    ToolName.MEMORY_READ: MemoryReadArgs,
}

_TOOL_CAPABILITY: dict[ToolName, Capability] = {
    ToolName.NEXUS_STATUS: Capability.NEXUS_STATUS_READ,
    ToolName.MEMORY_WRITE: Capability.MEMORY_WRITE,
    ToolName.MEMORY_READ: Capability.MEMORY_READ,
}


class ToolRequest(FrozenModel):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    request_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    correlation_id: str = Field(min_length=1, max_length=64)
    tool_name: ToolName
    capability: Capability
    arguments: ToolArguments
    requested_at: AwareDatetime = Field(default_factory=utcnow)
    deadline_at: AwareDatetime
    idempotency_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _deadline_after_request(self) -> ToolRequest:
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self

    @model_validator(mode="after")
    def _tool_arguments_capability_match(self) -> ToolRequest:
        expected_args = _TOOL_ARGUMENTS[self.tool_name]
        if not isinstance(self.arguments, expected_args):
            raise ValueError(f"{self.tool_name.value} requires {expected_args.__name__}")
        expected_capability = _TOOL_CAPABILITY[self.tool_name]
        if self.capability is not expected_capability:
            raise ValueError(
                f"{self.tool_name.value} requires capability {expected_capability.value}"
            )
        return self


class ToolResult(FrozenModel):
    """Envelope, not exception. Expected integration failures are typed here."""

    request_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    outcome: ToolOutcome
    data: CanonicalNexusStatus | MemoryWriteResult | MemoryReadResult | None = None
    error: JarvisError | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=8)
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime = Field(default_factory=utcnow)
    duration_ms: int = Field(default=0, ge=0)
    attempts: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def _outcome_payload(self) -> ToolResult:
        if self.outcome is ToolOutcome.SUCCESS and self.data is None:
            raise ValueError("SUCCESS requires data")
        if self.outcome is ToolOutcome.FAILURE and self.error is None:
            raise ValueError("FAILURE requires error")
        if self.outcome is ToolOutcome.SUCCESS and self.error is not None:
            raise ValueError("SUCCESS must not carry error")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self
