"""E2E: context grounding in the NEXUS response (Slice 2, T10).

Full chain over the real HTTP API: a stored memory about the equipment is
retrieved as context for the NEXUS question and rendered as an explicitly
NOT-verified "Você me disse:" note, while the facts stay measured.

Non-vacuity note: FTS5 uses AND semantics, so the memory text covers every
token of the NEXUS question; without that the memory would not be retrieved
and the test would prove nothing.
"""

from __future__ import annotations

import httpx
import pytest

from jarvis.api.main import create_app
from jarvis.config import Settings
from tests.conftest import StubNexusServer, _equipment_payload, _summary_payload


def _normal_payloads():
    return {
        "/api/v1/equipment": _equipment_payload(),
        "/api/v1/equipment/*": _summary_payload(),
        "/api/v1/simulation/status": {"running": False},
    }


@pytest.fixture()
def normal_stub():
    server = StubNexusServer(_normal_payloads()).start()
    yield server
    server.stop()


@pytest.fixture()
def grounding_client(db_url, normal_stub):
    settings = Settings(
        database_url=db_url,  # noqa: S106 - test fixture, not a credential
        nexus_base_url=normal_stub.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_e2e_memory_grounds_nexus_response(grounding_client):
    sid = (await grounding_client.post("/api/v1/sessions", json={})).json()["id"]

    r = await grounding_client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={
            "content": (
                "Lembre-se: como está o nexus na minha casa: meu equipamento é o Quadro Geral"
            )
        },
    )
    assert r.status_code == 200
    assert "Guardei na memória" in r.json()["message"]

    r = await grounding_client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "como está o nexus?"},
    )
    assert r.status_code == 200
    body = r.json()
    message = body["message"]

    # Facts stay measured and grounded...
    assert "NEXUS" in message
    assert body["status"] == "normal"
    # ...and the memory arrives as an explicitly NOT-verified note.
    assert "Você me disse:" in message
    assert "Quadro Geral" in message
