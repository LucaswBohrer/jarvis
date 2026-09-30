"""Composition root. Wires settings -> adapters -> orchestrator.

Nothing here is importable by the domain or application layers: the
dependency direction is api -> application -> ports <- adapters.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.llm.fake import FakeLLMProvider
from ..adapters.llm.openai import OpenAIProvider
from ..adapters.nexus.http import NexusAdapterConfig, NexusHttpAdapter
from ..adapters.persistence.database import Database
from ..adapters.persistence.repositories import (
    SqlAuditRepository,
    SqlCommitmentRepository,
    SqlMemoryRepository,
    SqlMessageRepository,
    SqlServiceMetaRepository,
    SqlSessionRepository,
    SqlTaskRepository,
)
from ..application.commitments import CommitmentService
from ..application.memory_service import MemoryService
from ..application.orchestrator import Orchestrator
from ..config import SCHEMA_HEAD, Settings
from ..observability.logging import get_logger
from ..ports.clock import SystemClock
from ..ports.llm import LLMProvider
from ..security.policy import PolicyEngine

log = get_logger(__name__)


@dataclass
class AppState:
    settings: Settings
    db: Database
    sessions: SqlSessionRepository
    messages: SqlMessageRepository
    tasks: SqlTaskRepository
    audit: SqlAuditRepository
    policy: PolicyEngine
    memory: MemoryService
    commitments: CommitmentService
    orchestrator: Orchestrator


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeLLMProvider()
    if settings.llm_provider == "openai":
        settings.validate_provider_ready()
        return OpenAIProvider(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            input_price_per_1m_usd=settings.llm_input_price_per_1m_usd,
            output_price_per_1m_usd=settings.llm_output_price_per_1m_usd,
        )
    raise ValueError(f"unknown llm_provider: {settings.llm_provider}")


def build_app_state(settings: Settings) -> AppState:
    settings.validate_provider_ready()
    policy = PolicyEngine.from_toml(settings.policy_file)

    db = Database(settings.database_url)
    db_file = db.db_file()
    if db_file is not None:
        db_file.parent.mkdir(parents=True, exist_ok=True)

    nexus_cfg = NexusAdapterConfig(
        base_url=settings.nexus_base_url,
        connect_timeout_ms=settings.nexus_connect_timeout_ms,
        read_timeout_ms=settings.nexus_read_timeout_ms,
        pool_timeout_ms=settings.nexus_pool_timeout_ms,
        total_deadline_ms=settings.nexus_total_deadline_ms,
        max_retries=settings.nexus_max_retries,
        circuit_failure_threshold=settings.nexus_circuit_failure_threshold,
        circuit_reset_seconds=settings.nexus_circuit_reset_seconds,
        max_response_bytes=settings.nexus_max_response_bytes,
    )
    nexus = NexusHttpAdapter(nexus_cfg)
    llm = build_llm_provider(settings)

    sessions = SqlSessionRepository(db)
    messages = SqlMessageRepository(db)
    tasks = SqlTaskRepository(db)
    audit = SqlAuditRepository(db)
    memory_repo = SqlMemoryRepository(db)
    memory = MemoryService(
        db=db,
        memory=memory_repo,
        audit=audit,
        policy=policy,
        clock=SystemClock(),
    )
    commitments = CommitmentService(
        db=db,
        commitments=SqlCommitmentRepository(db),
        memory=memory_repo,
        audit=audit,
        meta=SqlServiceMetaRepository(db),
        clock=SystemClock(),
    )
    orchestrator = Orchestrator(
        settings=settings,
        db=db,
        sessions=sessions,
        messages=messages,
        tasks=tasks,
        audit=audit,
        policy=policy,
        nexus=nexus,
        llm=llm,
        memory_service=memory,
        commitment_service=commitments,
        clock=SystemClock(),
    )
    return AppState(
        settings=settings,
        db=db,
        sessions=sessions,
        messages=messages,
        tasks=tasks,
        audit=audit,
        policy=policy,
        memory=memory,
        commitments=commitments,
        orchestrator=orchestrator,
    )


async def check_readiness(state: AppState) -> tuple[bool, str]:
    """Ready iff the DB schema is at head. NEXUS health never affects this."""
    try:
        ok = await state.db.schema_ok(SCHEMA_HEAD)
    except Exception as exc:  # unreadable/missing DB file
        return False, f"database unreachable: {type(exc).__name__}"
    if not ok:
        current = await state.db.schema_version()
        return False, f"schema behind: {current} < {SCHEMA_HEAD} (run alembic upgrade head)"
    return True, "ok"
