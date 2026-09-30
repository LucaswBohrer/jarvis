"""Port: injectable clock. Freshness checks and timestamps use this, never time.time() directly."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...


class SystemClock:
    """Production clock: timezone-aware UTC now."""

    def now(self) -> datetime:
        return datetime.now(UTC)
