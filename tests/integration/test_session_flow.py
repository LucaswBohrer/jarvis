"""Integration tests: F3.2 session flow.

Covers the browser -> POST /api/v1/sessions -> POST /messages -> orchestrator
-> structured response path, plus UI contract and security-boundary tests
for the web shell (thin client only: no policy, no NEXUS, no secrets in JS).

Driven over httpx ASGITransport. The NEXUS stub and a fake LLM keep the
round-trip deterministic without touching the real NEXUS.
"""

from __future__ import annotations

import re

import httpx
import pytest

from jarvis.api.main import create_app
from jarvis.config import Settings

# NOTE: the `db_url` and `stub_nexus` fixtures are provided by tests/conftest.py.


@pytest.fixture()
def api_settings(db_url, stub_nexus):
    return Settings(
        database_url=db_url,
        nexus_base_url=stub_nexus.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture()
def app(api_settings):
    return create_app(api_settings)


@pytest.fixture()
def client(app):
    # raise_app_exceptions=False: emulate the production ASGI server, where an
    # unexpected exception becomes a 500 JSON response instead of propagating.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create_session(client) -> str:
    r = await client.post("/api/v1/sessions")
    assert r.status_code == 201
    return r.json()["id"]


# -- session ----------------------------------------------------------------


async def test_create_session_returns_id(client):
    # Session creation: valid, contract-shaped.
    r = await client.post("/api/v1/sessions")
    assert r.status_code == 201
    body = r.json()
    assert isinstance(body["id"], str) and len(body["id"]) > 0


async def test_session_is_persisted(client):
    # A created session accepts a message: it was persisted, not invented.
    sid = await _create_session(client)
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages", json={"content": "Como está o NEXUS?"}
    )
    assert r.status_code == 200


async def test_message_to_unknown_session_404(client):
    r = await client.post(
        "/api/v1/sessions/does-not-exist/messages", json={"content": "Como está o NEXUS?"}
    )
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message_key"] == "session.not_found"


# -- message ----------------------------------------------------------------


async def test_send_message_round_trip(client):
    # Full flow: browser -> session -> message -> orchestrator -> response.
    # FakeLLM + stub NEXUS keep it deterministic.
    sid = await _create_session(client)
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages", json={"content": "Como está o NEXUS?"}
    )
    assert r.status_code == 200
    body = r.json()
    for field in ("task_id", "status", "message", "source"):
        assert field in body, f"missing {field}"
    assert body["task_id"]
    assert body["message"]


async def test_messages_are_session_scoped(client):
    # Two sessions, two independent round-trips.
    a = await _create_session(client)
    b = await _create_session(client)
    ra = await client.post(f"/api/v1/sessions/{a}/messages", json={"content": "Como está o NEXUS?"})
    rb = await client.post(f"/api/v1/sessions/{b}/messages", json={"content": "Como está o NEXUS?"})
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json()["task_id"] != rb.json()["task_id"]


async def test_empty_message_rejected(client):
    sid = await _create_session(client)
    r = await client.post(f"/api/v1/sessions/{sid}/messages", json={"content": ""})
    assert r.status_code == 422


async def test_whitespace_message_rejected(client):
    sid = await _create_session(client)
    r = await client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "   "})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INPUT_INVALID"


async def test_oversize_message_rejected(client):
    sid = await _create_session(client)
    # Beyond the UserMessage contract (2000) but inside the API envelope (4000).
    r = await client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "x" * 2001})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INPUT_INVALID"
    # Beyond the API envelope itself.
    r = await client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "x" * 4001})
    assert r.status_code == 422


async def test_internal_error_never_becomes_success(app, client, monkeypatch):
    # A crashing orchestrator must surface as 500, never as a fake success.
    async def boom(**kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(app.state.jarvis.orchestrator, "handle_message", boom)
    sid = await _create_session(client)
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages", json={"content": "Como está o NEXUS?"}
    )
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "message" not in body  # no AssistantResponse shape on failure


# -- cancel endpoint contract (backend supports it; UI aborts in-flight) ----


async def test_cancel_unknown_task_404(client):
    r = await client.post("/api/v1/tasks/nope/cancel")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# -- UI contract: thin client -----------------------------------------------


async def test_page_creates_session(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert 'fetch("/api/v1/sessions"' in r.text


async def test_page_sends_message(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert '"/messages"' in r.text


async def test_page_renders_api_response(client):
    # The page renders data.message from the real API payload (textContent,
    # never innerHTML).
    r = await client.get("/")
    assert r.status_code == 200
    assert "payload.message" in r.text
    assert "textContent" in r.text
    assert "innerHTML" not in r.text


async def test_page_handles_errors(client):
    # API error payload is surfaced; network failure shows "indisponível".
    r = await client.get("/")
    assert r.status_code == 200
    assert "message_key" in r.text
    assert "JARVIS indisponível." in r.text


async def test_page_self_contained(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "<script src" not in r.text
    assert "<link" not in r.text
    assert "http://" not in r.text and "https://" not in r.text


async def test_page_has_no_secrets(client):
    r = await client.get("/")
    assert r.status_code == 200
    lowered = r.text.lower()
    # NOTE (F3.7 correction): the bare "sk-" prefix false-positives on
    # harmless identifiers (e.g. the "task-list" element id), so the
    # key-pattern check targets a real key shape instead.
    for token in ("api_key", "apikey", "bearer", "password", "secret"):
        assert token not in lowered, f"forbidden token in UI: {token}"
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", r.text)


# -- security boundary: browser never talks to NEXUS -------------------------


async def test_page_never_calls_nexus_directly(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "localhost:8000" not in r.text
    assert "127.0.0.1:8000" not in r.text
    assert ":8000" not in r.text
    assert "/api/v1/equipment" not in r.text
