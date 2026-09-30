"""E2E: vertical slice "JARVIS, como está o NEXUS?" (§70-73 do desenho).

Full chain over real sockets: FastAPI app via ASGITransport ->
StubNexusServer on 127.0.0.1 -> FakeLLMProvider -> real temp SQLite.
Verifies response + DB rows + audit entries together.
"""

from __future__ import annotations

import httpx
import pytest

from jarvis.adapters.persistence.database import Database
from jarvis.adapters.persistence.repositories import (
    SqlAuditRepository,
    SqlMessageRepository,
    SqlSessionRepository,
    SqlTaskRepository,
)
from jarvis.api.main import create_app
from jarvis.config import Settings
from jarvis.domain.contracts.task import TaskState
from tests.conftest import StubNexusServer, _equipment_payload, _summary_payload


def _critical_payloads():
    summary = _summary_payload()
    summary["last_reading"]["status"] = "critical"
    summary["diagnosis"] = {
        "status": "critical",
        "severity": "critical",
        "anomalies": [
            {"code": "V-DROP", "message": "Queda de tensão detectada", "severity": "medium"},
            {
                "code": "OVERHEAT",
                "message": "Superaquecimento no barramento",
                "severity": "critical",
            },
        ],
        "recommendations": [
            {"id": "r1", "title": "Verificar disjuntor", "message": "Inspecione o disjuntor geral."}
        ],
    }
    summary["active_events"] = 2
    return {
        "/api/v1/equipment": _equipment_payload(),
        "/api/v1/equipment/*": summary,
        "/api/v1/simulation/status": {"running": True, "mode": "fault_injection"},
    }


@pytest.fixture()
def critical_stub():
    server = StubNexusServer(_critical_payloads()).start()
    yield server
    server.stop()


@pytest.fixture()
def e2e_client(db_url, critical_stub):
    settings = Settings(
        database_url=db_url,
        nexus_base_url=critical_stub.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _repos(db_url):
    db = Database(db_url)
    return {
        "db": db,
        "sessions": SqlSessionRepository(db),
        "messages": SqlMessageRepository(db),
        "tasks": SqlTaskRepository(db),
        "audit": SqlAuditRepository(db),
    }


async def test_e2e_critical_slice_response_db_audit(e2e_client, db_url):
    """Critical + simulation: response names anomalies, DB + audit consistent."""
    repos = _repos(db_url)
    try:
        r = await e2e_client.post("/api/v1/sessions", json={})
        assert r.status_code == 201
        sid = r.json()["id"]

        r = await e2e_client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "JARVIS, como está o NEXUS?"},
        )
        assert r.status_code == 200
        body = r.json()
        # response: critical, both anomalies, recommendation, simulation disclosed
        assert body["status"] in ("completed", "critical", "simulation")
        assert "Queda de tensão detectada" in body["message"]
        assert "Superaquecimento no barramento" in body["message"]
        assert "simula" in body["message"].lower()
        task_id = body["task_id"]

        # DB: task COMPLETED with a response
        task = await repos["tasks"].get(task_id)
        assert task is not None and task.state is TaskState.COMPLETED
        assert task.result is not None

        # DB: assistant message persisted and linked
        assistant_msgs = await repos["messages"].list_assistant_by_task(task_id)
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].content == body["message"]

        # audit: full ordered event chain, summaries only (no raw payloads)
        events = await repos["audit"].list_by_task(task_id)
        types = [e.event_type.value for e in events]
        assert types == [
            "task.created",
            "task.planned",
            "policy.decided",
            "tool.started",
            "tool.completed",
            "verification.completed",
            "context.assembled",
            "llm.completed",
            "task.completed",
        ]
        for e in events:
            blob = (e.request_summary or "") + (e.result_summary or "")
            assert "power_factor" not in blob and "2480" not in blob
    finally:
        await repos["db"].close()


async def test_e2e_second_message_reuses_session(e2e_client, db_url):
    """Conversation continuity: two messages, two tasks, one session."""
    repos = _repos(db_url)
    try:
        sid = (await e2e_client.post("/api/v1/sessions", json={})).json()["id"]
        r1 = await e2e_client.post(
            f"/api/v1/sessions/{sid}/messages", json={"content": "como está o nexus?"}
        )
        r2 = await e2e_client.post(
            f"/api/v1/sessions/{sid}/messages", json={"content": "e o nexus, como está agora?"}
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["task_id"] != r2.json()["task_id"]
        history = await repos["messages"].list_by_session(sid)
        assert len(history) == 4  # 2 user + 2 assistant
    finally:
        await repos["db"].close()
