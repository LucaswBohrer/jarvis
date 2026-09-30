"""JSON logging on stdlib. No third-party logging dependency in Phase 1.

A ContextVar carries the correlation id so every log line inside a request
can be tied back to it without threading it through every signature.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jarvis_correlation_id", default="-"
)


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the root handler once. Safe to call repeatedly (idempotent)."""
    root = logging.getLogger()
    if getattr(root, "_jarvis_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
            )
        )
    handler.addFilter(_CorrelationFilter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Quiet the chatty libraries; our own loggers stay at the chosen level.
    for noisy in ("uvicorn", "uvicorn.access", "httpx", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._jarvis_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
