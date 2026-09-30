"""`python -m jarvis` — start the Phase 1 API server (loopback only)."""

from __future__ import annotations

import uvicorn

from .api.main import VERSION, create_app
from .config import get_settings
from .observability.logging import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log.info(
        "starting jarvis %s on %s:%s (llm=%s, nexus=%s)",
        VERSION,
        settings.host,
        settings.port,
        settings.llm_provider,
        settings.nexus_base_url,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="warning",  # our own JSON logging handles access lines
    )


if __name__ == "__main__":
    main()
