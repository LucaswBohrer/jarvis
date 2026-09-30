"""Integration tests: F3.6 NEXUS demonstration.

Acceptance (PHASE3_PROPOSAL §8): "Como está o NEXUS?" works end to end
through the web UI: intent -> policy -> NexusIntegration (real data when
the NEXUS answers) -> verification -> renderer -> UI. When the NEXUS is
down, the UI shows an honest fallback -- never invented data, never a
false "tudo normal".

F3.6 contract gap (fixed alongside these tests): the adapter's boundary
DTOs rejected the REAL NEXUS shapes -- integer equipment ids (SQLite
rowids), the summary's nested equipment object, and plain-string
anomalies/recommendations from the diagnosis engine. A healthy NEXUS
would have produced NEXUS_CONTRACT_INVALID and been reported as
"indisponível". The DTOs now normalize those shapes at the boundary
(canonical contracts stay strict); these tests serve the REAL shapes.

No real network calls: the NEXUS is mocked over httpx.MockTransport
(adapter level) or over loopback sockets (StubNexusServer, app level).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from jarvis.adapters.nexus.http import NexusAdapterConfig, NexusHttpAdapter
from jarvis.api.main import create_app
from jarvis.config import Settings
from jarvis.domain.contracts.common import new_id, utcnow
from jarvis.domain.contracts.nexus import (
    CanonicalNexusStatus,
    NexusAnomalyDTO,
    NexusEquipmentDTO,
    NexusRecommendationDTO,
    NexusStatusQuery,
    NexusSummaryDTO,
)
from jarvis.domain.contracts.tool import (
    Capability,
    ToolName,
    ToolOutcome,
    ToolRequest,
)
from jarvis.domain.errors import ErrorCode
from tests.conftest import StubNexusServer, make_nexus_handler

INDEX_HTML = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "jarvis"
    / "api"
    / "static"
    / "index.html"
)


# ---------------------------------------------------------------------------
# Real NEXUS shapes (read from ~/workspace/nexus, branch muse/nexus-2.0-phase-2.4).
# ---------------------------------------------------------------------------


def _real_equipment() -> dict[str, Any]:
    # NEXUS GET /api/v1/equipment -> {"equipment": [sqlite rows]}: id is int.
    return {
        "equipment": [
            {
                "id": 1,
                "code": "DEFAULT",
                "name": "Quadro Geral",
                "description": "Quadro principal",
                "equipment_type": "panel",
                "location": "Sala elétrica",
                "status": "active",
                "enabled": True,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            },
            {
                "id": 2,
                "code": "EQ-2",
                "name": "Quadro Secundário",
                "description": "",
                "equipment_type": "panel",
                "location": "Galpão",
                "status": "active",
                "enabled": True,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            },
        ]
    }


def _real_summary() -> dict[str, Any]:
    # NEXUS GET /api/v1/equipment/{id}/summary: "equipment" is the full
    # object; anomalies/recommendations are plain strings; timestamps are
    # tz-aware UTC ISO-8601 strings.
    now_iso = datetime.now(UTC).isoformat()
    return {
        "equipment": _real_equipment()["equipment"][0],
        "last_reading": {
            "timestamp": now_iso,
            "voltage": 220.5,
            "current": 10.2,
            "frequency": 60.0,
            "power_factor": 0.92,
            "active_power": 2067.0,
            "temperature": 38.5,
            "status": "normal",
        },
        "last_reading_at": now_iso,
        "diagnosis": {
            "status": "warning",
            "severity": "warning",
            "anomalies": ["HIGH_VOLTAGE"],
            "recommendations": ["Inspect voltage regulation and supply conditions."],
        },
        "active_events": 2,
        "open_episodes": 1,
        "readings_count": 150,
    }


def _real_simulation() -> dict[str, Any]:
    # NEXUS GET /api/v1/simulation/status?equipment_id=... (idle state).
    return {
        "running": False,
        "session_id": None,
        "mode": "normal",
        "intensity": 100.0,
        "anomalies": [],
        "started_at": None,
        "ends_at": None,
        "remaining_seconds": 0,
        "equipment_id": 1,
    }


def _request() -> ToolRequest:
    return ToolRequest(
        task_id=new_id(),
        correlation_id=new_id(),
        tool_name=ToolName.NEXUS_STATUS,
        capability=Capability.NEXUS_STATUS_READ,
        arguments=NexusStatusQuery(equipment_code="DEFAULT"),
        deadline_at=utcnow() + timedelta(seconds=10),
    )


def _adapter(handler: Any = None, **cfg_over: Any) -> NexusHttpAdapter:
    cfg = NexusAdapterConfig(base_url="http://127.0.0.1:8000", **cfg_over)
    transport = httpx.MockTransport(handler) if handler is not None else None
    return NexusHttpAdapter(cfg, transport=transport)


def _real_handler() -> Any:
    return make_nexus_handler(
        equipment=_real_equipment(),
        summary=_real_summary(),
        simulation=_real_simulation(),
    )


# ---------------------------------------------------------------------------
# Case 1: NEXUS available with the REAL contract -> real data flows through.
# ---------------------------------------------------------------------------


async def test_real_contract_shapes_produce_canonical_status() -> None:
    handler = _real_handler()
    ad = _adapter(handler)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.SUCCESS, result.error
    data = result.data
    assert isinstance(data, CanonicalNexusStatus)
    # Boundary normalized int id -> str; canonical contracts unchanged.
    assert data.equipment.code == "DEFAULT"
    assert data.equipment.name == "Quadro Geral"
    # Real electrical values survive the pipeline (never invented, never dropped).
    assert data.reading is not None
    assert data.reading.voltage_v == pytest.approx(220.5)
    assert data.reading.current_a == pytest.approx(10.2)
    # Real diagnosis: plain-string anomaly preserved as the message.
    assert data.diagnosis is not None
    assert data.diagnosis.severity.value == "warning"
    assert [a.message for a in data.diagnosis.anomalies] == ["HIGH_VOLTAGE"]
    assert [r.text for r in data.diagnosis.recommendations] == [
        "Inspect voltage regulation and supply conditions."
    ]
    assert handler.calls["n"] == 3  # equipment + summary + simulation


async def test_legacy_assumed_shapes_still_accepted() -> None:
    # The boundary stays tolerant both ways: the older assumed shapes
    # (str ids, str equipment ref) keep working.
    ad = _adapter(make_nexus_handler())
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.SUCCESS, result.error


def test_dto_normalization_units() -> None:
    eq = NexusEquipmentDTO.model_validate(
        {"id": 7, "code": "X", "name": "N", "status": "active", "enabled": True}
    )
    assert eq.id == "7"
    s = NexusSummaryDTO.model_validate({"equipment": {"id": 7, "code": "X"}})
    assert s.equipment == "7"
    a = NexusAnomalyDTO.model_validate("HIGH_VOLTAGE")
    assert a.message == "HIGH_VOLTAGE" and a.code is None
    r = NexusRecommendationDTO.model_validate("Check the wiring.")
    assert r.message == "Check the wiring." and r.title is None
    # Structured dicts are untouched.
    a2 = NexusAnomalyDTO.model_validate({"code": "HV", "message": "high", "severity": "warning"})
    assert (a2.code, a2.message, a2.severity) == ("HV", "high", "warning")


# ---------------------------------------------------------------------------
# Cases 2-4: failure modes are explicit and honest, never false health.
# ---------------------------------------------------------------------------


async def test_connection_refused_is_unavailable_not_healthy() -> None:
    def refusing(request: httpx.Request) -> Any:
        raise httpx.ConnectError("refused")

    ad = _adapter(refusing, max_retries=1)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.data is None
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_UNAVAILABLE


async def test_timeout_is_controlled() -> None:
    handler = make_nexus_handler(equipment=_real_equipment(), summary=_real_summary(), sleep_s=5)
    ad = _adapter(handler, read_timeout_ms=200, total_deadline_ms=800, max_retries=0)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_TIMEOUT


async def test_http_500_is_failure_without_partial_data() -> None:
    # A bare 500 is not in the retryable 502/503/504 set: the adapter
    # treats it as an unexpected response (contract_invalid), never as
    # healthy data. Either way the UI fallback stays honest.
    handler = make_nexus_handler(
        equipment=_real_equipment(),
        summary=_real_summary(),
        status_overrides={"/api/v1/equipment": 500},
    )
    ad = _adapter(handler, max_retries=0)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.data is None  # never a partial payload
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_CONTRACT_INVALID


# ---------------------------------------------------------------------------
# Case 5: invalid payload -> contract error, never treated as healthy.
# ---------------------------------------------------------------------------


async def test_invalid_payload_is_contract_error_not_healthy() -> None:
    bad_summary = _real_summary()
    bad_summary["equipment"] = 12345  # neither str nor object: unresolvable
    handler = make_nexus_handler(equipment=_real_equipment(), summary=bad_summary)
    ad = _adapter(handler)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.data is None
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_CONTRACT_INVALID


# ---------------------------------------------------------------------------
# Case 6: UI flow -- "Como está o NEXUS?" through the real app (F3.5 pattern).
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_stub_nexus() -> Iterator[StubNexusServer]:
    server = StubNexusServer(
        {
            "/api/v1/equipment": _real_equipment(),
            "/api/v1/equipment/*": _real_summary(),
            "/api/v1/simulation/status": _real_simulation(),
        }
    ).start()
    yield server
    server.stop()


@pytest.fixture()
def ui_settings(db_url: str, real_stub_nexus: StubNexusServer) -> Settings:
    return Settings(
        database_url=db_url,
        nexus_base_url=real_stub_nexus.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture()
def ui_app(ui_settings: Settings) -> FastAPI:
    return create_app(ui_settings)


@pytest.fixture()
def ui_client(ui_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=ui_app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _post_nexus_question(ui_client: httpx.AsyncClient) -> dict[str, Any]:
    r = await ui_client.post("/api/v1/sessions")
    assert r.status_code == 201, r.text
    sid = str(r.json()["id"])
    r = await ui_client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Como está o NEXUS?"},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_ui_nexus_question_shows_real_data(ui_client: httpx.AsyncClient) -> None:
    # The exact flow the web shell drives: POST /messages, render payload.message.
    body = await _post_nexus_question(ui_client)
    message = str(body["message"])
    assert "indisponível" not in message.lower()
    assert "Quadro Geral" in message  # real equipment name from the NEXUS
    assert "220" in message  # real voltage rendered
    # The page renders exactly this field via textContent (F3.2/F3.3 contract).
    page = INDEX_HTML.read_text(encoding="utf-8")
    assert "payload.message" in page
    assert "textContent" in page


async def test_ui_nexus_down_shows_honest_fallback(db_url: str, ui_app: FastAPI) -> None:
    # NEXUS unreachable (nothing on the port): the UI must say so honestly.
    closed_port_settings = Settings(
        database_url=db_url,
        nexus_base_url="http://127.0.0.1:1",  # nothing listens here
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )
    app = create_app(closed_port_settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/sessions")
        assert r.status_code == 201, r.text
        sid = str(r.json()["id"])
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "Como está o NEXUS?"},
        )
        assert r.status_code == 200, r.text
        message = str(r.json()["message"]).lower()
        assert "indisponível" in message
        assert "220" not in message  # no invented values
