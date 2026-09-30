"""Port: LLM provider. The core only knows this interface.

The provider generates a structured response *plan*; it never executes tools,
never authorizes, never produces factual prose. Cancellation is cooperative:
the caller cancels the coroutine and the adapter must propagate CancelledError.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.contracts.llm import LLMRequest, LLMResponse


class LLMError(Exception):
    """Base for provider failures. The orchestrator maps these to audited fallback."""


class LLMTimeoutError(LLMError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMMalformedError(LLMError):
    pass


class LLMProvider(Protocol):
    name: str

    async def generate_structured(
        self, request: LLMRequest, *, timeout_seconds: float
    ) -> LLMResponse:
        """Produce a validated NexusResponsePlan. Raises on timeout/cancel/malformed."""
        ...

    async def health(self) -> bool:
        """Provider configured and reachable. Never called by /ready."""
        ...

    async def close(self) -> None:
        """Release clients/connections."""
        ...
