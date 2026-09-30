"""Shared fixtures for the JARVIS Phase 1 test suite.

Every test gets a fresh migrated SQLite DB (alembic upgrade head) in
tmp_path. Nothing here touches ~/workspace/nexus or the real NEXUS.
"""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from jarvis.adapters.llm.fake import FakeBehavior, FakeLLMProvider  # noqa: E402
from jarvis.adapters.nexus.http import NexusAdapterConfig, NexusHttpAdapter  # noqa: E402
from jarvis.adapters.persistence.database import Database  # noqa: E402
from jarvis.adapters.persistence.repositories import (  # noqa: E402
    SqlAuditRepository,
    SqlCommitmentRepository,
    SqlMemoryRepository,
    SqlMessageRepository,
    SqlServiceMetaRepository,
    SqlSessionRepository,
    SqlTaskRepository,
)
from jarvis.application.commitments import CommitmentService  # noqa: E402
from jarvis.application.memory_service import MemoryService  # noqa: E402
from jarvis.application.orchestrator import Orchestrator  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.domain.contracts.session import SessionRecord  # noqa: E402
from jarvis.ports.clock import SystemClock  # noqa: E402
from jarvis.security.policy import PolicyEngine  # noqa: E402


def _summary_payload(**over):
    # Timestamps are generated per call, not frozen at import: the stub must
    # serve fresh readings, otherwise the 10s freshness window marks every
    # slow-suite run as STALE (pre-existing flake).
    now_iso = datetime.now(UTC).isoformat()
    payload = {
        "equipment": "eq-1",
        "last_reading": {
            "timestamp": now_iso,
            "voltage": 220.0,
            "current": 10.0,
            "frequency": 60.0,
            "power_factor": 0.9,
            "active_power": 1980.0,
            "temperature": 40.0,
            "status": "normal",
        },
        "last_reading_at": now_iso,
        "diagnosis": {
            "status": "normal",
            "severity": "info",
            "anomalies": [],
            "recommendations": [],
        },
        "active_events": 0,
        "open_episodes": 0,
        "readings_count": 50,
    }
    payload.update(over)
    return payload


def _equipment_payload(**over):
    payload = {
        "equipment": [
            {
                "id": "eq-1",
                "code": "DEFAULT",
                "name": "Quadro Geral",
                "status": "active",
                "enabled": True,
            }
        ]
    }
    payload.update(over)
    return payload


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    """Fresh migrated DB. Returns the SQLAlchemy URL."""
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("JARVIS_DATABASE_URL", url)
    cfg = Config(str(REPO / "alembic.ini"))
    command.upgrade(cfg, "head")
    return url


@pytest.fixture()
def settings(db_url):
    return Settings(
        database_url=db_url,
        nexus_base_url="http://127.0.0.1:8000",
        llm_provider="fake",
        nexus_freshness_seconds=60,
        llm_timeout_seconds=15.0,
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture()
async def repos(db_url):
    """Repository bundle over a fresh migrated DB.

    The engine is disposed in teardown: aiosqlite keeps a worker thread per
    pooled connection, and if it outlives the test's event loop the thread
    raises ``RuntimeError: Event loop is closed`` (surfaced as
    PytestUnhandledThreadExceptionWarning, noisy on Windows). Closing here —
    before the loop ends — is the correct lifecycle; ``Database.close()`` is
    idempotent so tests that close ``repos["db"]`` themselves are unaffected.
    """
    db = Database(db_url)
    try:
        yield {
            "db": db,
            "sessions": SqlSessionRepository(db),
            "messages": SqlMessageRepository(db),
            "tasks": SqlTaskRepository(db),
            "audit": SqlAuditRepository(db),
            "memory": SqlMemoryRepository(db),
            "commitments": SqlCommitmentRepository(db),
            "service_meta": SqlServiceMetaRepository(db),
        }
    finally:
        await db.close()


@pytest.fixture()
def policy():
    return PolicyEngine.from_toml(str(REPO / "config" / "policy.toml"))


def make_nexus_handler(
    *,
    equipment: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    simulation: dict[str, Any] | None = None,
    sleep_s: float = 0.0,
    status_overrides: dict[str, int] | None = None,
    raw_overrides: dict[str, bytes] | None = None,
) -> Any:
    """httpx.MockTransport handler serving the 3 NEXUS endpoints.

    status_overrides: {path_prefix: status_code}; raw_overrides: {path_prefix: bytes}.
    """
    calls = {"n": 0, "paths": []}
    equipment = equipment if equipment is not None else _equipment_payload()
    summary = summary if summary is not None else _summary_payload()
    simulation = simulation if simulation is not None else {"running": False}

    def handler(request):
        import time

        calls["n"] += 1
        calls["paths"].append(request.url.path)
        if sleep_s:
            time.sleep(sleep_s)
        path = request.url.path
        if raw_overrides:
            for prefix, raw in raw_overrides.items():
                if path.startswith(prefix):
                    return httpx.Response(200, content=raw)
        if status_overrides:
            for prefix, code in status_overrides.items():
                if path.startswith(prefix):
                    return httpx.Response(code, json={"detail": "err"})
        if path == "/api/v1/equipment":
            return httpx.Response(200, json=equipment)
        if path.endswith("/summary"):
            return httpx.Response(200, json=summary)
        if path.startswith("/api/v1/simulation/status"):
            return httpx.Response(200, json=simulation)
        return httpx.Response(404, json={"detail": "not found"})

    handler.calls = calls
    return handler


@pytest.fixture()
def nexus_handler():
    return make_nexus_handler()


def build_orchestrator(settings, repos, policy, handler=None, llm_behavior=FakeBehavior.VALID):
    nexus = NexusHttpAdapter(
        NexusAdapterConfig(base_url="http://127.0.0.1:8000"),
        transport=httpx.MockTransport(handler or make_nexus_handler()),
    )
    llm = FakeLLMProvider(llm_behavior)
    memory = MemoryService(
        db=repos["db"],
        memory=repos["memory"],
        audit=repos["audit"],
        policy=policy,
        clock=SystemClock(),
    )
    commitments = CommitmentService(
        db=repos["db"],
        commitments=repos["commitments"],
        memory=repos["memory"],
        audit=repos["audit"],
        meta=repos["service_meta"],
        clock=SystemClock(),
    )
    return Orchestrator(
        settings=settings,
        db=repos["db"],
        sessions=repos["sessions"],
        messages=repos["messages"],
        tasks=repos["tasks"],
        audit=repos["audit"],
        policy=policy,
        nexus=nexus,
        llm=llm,
        memory_service=memory,
        commitment_service=commitments,
    )


@pytest.fixture()
def orchestrator(settings, repos, policy, nexus_handler):
    return build_orchestrator(settings, repos, policy, nexus_handler)


@pytest.fixture()
def memory_service(repos, policy):
    return MemoryService(
        db=repos["db"],
        memory=repos["memory"],
        audit=repos["audit"],
        policy=policy,
        clock=SystemClock(),
    )


@pytest.fixture()
def commitment_service(repos):
    return CommitmentService(
        db=repos["db"],
        commitments=repos["commitments"],
        memory=repos["memory"],
        audit=repos["audit"],
        meta=repos["service_meta"],
        clock=SystemClock(),
    )


async def new_session_id(sessions):
    record = SessionRecord()
    await sessions.create(record)
    return record.id


class StubNexusServer:
    """Real loopback HTTP stub (for e2e over actual sockets)."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        self._payloads = payloads
        self._server = None
        self.port = 0

    def _make_handler(self):
        import json as _json

        payloads = self._payloads

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                key = self.path.split("?")[0]
                hit = None
                for prefix, payload in payloads.items():
                    if key == prefix or (prefix.endswith("*") and key.startswith(prefix[:-1])):
                        hit = payload
                        break
                if hit is None:
                    body = b'{"detail":"not found"}'
                    self.send_response(404)
                else:
                    body = _json.dumps(hit).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _H

    def start(self) -> StubNexusServer:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture()
def stub_nexus():
    server = StubNexusServer(
        {
            "/api/v1/equipment": _equipment_payload(),
            "/api/v1/equipment/*": _summary_payload(),
            "/api/v1/simulation/status": {"running": True, "mode": "simulation"},
        }
    ).start()
    yield server
    server.stop()
