"""Port: NEXUS integration. Knows nothing about sessions, tasks, LLM, or templates.

It translates NEXUS HTTP into canonical JARVIS data and classifies failures
as typed ToolResult values. The caller (orchestrator) evaluates policy BEFORE
invoking this port.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.contracts.tool import ToolRequest, ToolResult


class NexusIntegration(Protocol):
    async def get_status(self, request: ToolRequest) -> ToolResult:
        """Execute the read-only NEXUS status capability within request.deadline_at.

        Must enforce: GET only, /api/v1/ prefix, loopback destination,
        redirects disabled, max response bytes, total deadline, single retry
        for idempotent failures, circuit breaker. Returns a typed ToolResult —
        never raises for expected integration failures.
        """
        ...

    async def close(self) -> None:
        """Release HTTP client resources."""
        ...
