"""In-memory circuit breaker for the NEXUS integration.

CLOSED -> normal calls; success resets the failure count.
OPEN   -> fail fast with NEXUS_CIRCUIT_OPEN, no HTTP.
HALF_OPEN -> one probe call; success closes, failure re-opens.

State is per-process (Phase 1). No Redis, no distributed state.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 15.0) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_seconds <= 0:
            raise ValueError("reset_seconds must be > 0")
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    async def can_execute(self) -> bool:
        """True when a call may proceed. Transitions OPEN->HALF_OPEN when the
        reset interval has elapsed (single probe)."""
        async with self._lock:
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN:
                assert self._opened_at is not None
                if time.monotonic() - self._opened_at >= self._reset_seconds:
                    self._state = BreakerState.HALF_OPEN
                    return True
                return False
            return True  # HALF_OPEN: the probe itself

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
