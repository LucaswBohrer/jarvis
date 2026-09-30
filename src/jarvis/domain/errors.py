"""Typed error contract. Stable codes outside, safe details inside."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .contracts.common import FrozenModel


class ErrorCategory(str, Enum):
    VALIDATION = "validation"
    POLICY = "policy"
    INTEGRATION = "integration"
    RESILIENCE = "resilience"
    PROVIDER = "provider"
    VERIFICATION = "verification"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"


class ErrorCode(str, Enum):
    INPUT_INVALID = "INPUT_INVALID"
    INTENT_UNSUPPORTED = "INTENT_UNSUPPORTED"
    POLICY_DENIED = "POLICY_DENIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    NEXUS_TIMEOUT = "NEXUS_TIMEOUT"
    NEXUS_UNAVAILABLE = "NEXUS_UNAVAILABLE"
    NEXUS_CONTRACT_INVALID = "NEXUS_CONTRACT_INVALID"
    NEXUS_EQUIPMENT_NOT_FOUND = "NEXUS_EQUIPMENT_NOT_FOUND"
    NEXUS_CIRCUIT_OPEN = "NEXUS_CIRCUIT_OPEN"
    NEXUS_INVALID_DESTINATION = "NEXUS_INVALID_DESTINATION"
    NEXUS_RESPONSE_TOO_LARGE = "NEXUS_RESPONSE_TOO_LARGE"
    NEXUS_REDIRECT_BLOCKED = "NEXUS_REDIRECT_BLOCKED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    RESPONSE_GROUNDING_FAILED = "RESPONSE_GROUNDING_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_CONFLICT = "TASK_CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    MEMORY_SECRET_DETECTED = "MEMORY_SECRET_DETECTED"  # noqa: S105 - error code, not a credential
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorSource(str, Enum):
    USER = "user"
    NEXUS = "nexus"
    LLM = "llm"
    POLICY = "policy"
    SYSTEM = "system"


class JarvisError(FrozenModel):
    """Stable, user-safe error contract.

    The original cause and stack trace stay in the local technical log only;
    they are never part of this contract and never reach the user.
    """

    code: ErrorCode
    category: ErrorCategory
    user_message_key: str = Field(min_length=1, max_length=128)
    retryable: bool = False
    source: ErrorSource = ErrorSource.SYSTEM
    correlation_id: str = Field(min_length=1, max_length=64)
    safe_context: dict[str, str] = Field(default_factory=dict)


class JarvisException(Exception):
    """Internal exception carrying a typed JarvisError.

    Raised for bugs, cancellation, and broken invariants at adapter/orchestrator
    boundaries. Expected integration failures are returned as typed ToolResult
    values, not raised.
    """

    def __init__(self, error: JarvisError) -> None:
        super().__init__(f"{error.code.value}: {error.user_message_key}")
        self.error = error
