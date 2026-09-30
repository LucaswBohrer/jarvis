"""In-memory metrics. Phase 1 has no Prometheus dependency.

Counters are enough for a local single-user service: request volumes,
task outcomes, and tool/LLM fallbacks. A snapshot is logged periodically
by the operator, not exposed over HTTP (no /metrics endpoint in Phase 1).
"""

from __future__ import annotations

import threading


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def inc(self, name: str, value: int = 1, **labels: str) -> None:
        key = name
        if labels:
            suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
            key = f"{name}{{{suffix}}}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


METRICS = Metrics()
