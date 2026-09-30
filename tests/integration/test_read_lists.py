"""Integration tests: F3.7 read-only list endpoints.

GET /api/v1/sessions, GET /api/v1/tasks, GET /api/v1/audit exist solely to
feed the F3.7 read-only views (Sessions, Tasks, Activity, Audit). They are
pure reads: no policy, no LLM, no NEXUS, no audit writes, no mutations.
"""

from __future__ import annotations

import httpx
import pytest

from jarvis.api.main import create_app
from jarvis.config import Settings


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
def client(api_settings):
    transport = httpx.ASGITransport(app=create_app(api_settings))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_sessions_list_empty(client):
    r = await client.get("/api/v1/sessions")
    assert r.status_code == 200
    assert r.json() == {"items": []}


async def test_sessions_list_newest_first(client):
    first = (await client.post("/api/v1/sessions")).json()["id"]
    second = (await client.post("/api/v1/sessions")).json()["id"]
    r = await client.get("/api/v1/sessions")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == [second, first]
    for item in items:
        assert set(item) == {"id", "status", "created_at", "updated_at"}
        assert item["status"] == "active"


async def test_sessions_list_limit_validated(client):
    r = await client.get("/api/v1/sessions?limit=51")
    assert r.status_code == 422
    r = await client.get("/api/v1/sessions?limit=1")
    assert r.status_code == 200


async def test_tasks_list_empty(client):
    r = await client.get("/api/v1/tasks")
    assert r.status_code == 200
    assert r.json() == {"items": []}


async def test_tasks_list_after_conversation_turn(client, api_settings):
    # A real turn creates a real task; the list shows its true state.
    sid = (await client.post("/api/v1/sessions")).json()["id"]
    turn = await client.post(
        f"/api/v1/sessions/{sid}/messages", json={"content": "lembre-se de que eu gosto de café"}
    )
    assert turn.status_code == 200
    task_id = turn.json()["task_id"]
    r = await client.get("/api/v1/tasks")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == task_id
    assert item["session_id"] == sid
    assert item["state"] in (
        "pending",
        "planning",
        "running",
        "waiting_confirmation",
        "completed",
        "failed",
        "cancelled",
    )
    assert set(item) == {
        "id",
        "session_id",
        "kind",
        "state",
        "current_step",
        "error_code",
        "created_at",
        "updated_at",
    }


async def test_tasks_list_limit_validated(client):
    r = await client.get("/api/v1/tasks?limit=0")
    assert r.status_code == 422


async def test_audit_list_newest_first(client, api_settings):
    # Drive two real turns so the audit log has real events.
    sid = (await client.post("/api/v1/sessions")).json()["id"]
    await client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "olá"})
    await client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "tudo bem?"})
    r = await client.get("/api/v1/audit?limit=200")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 2
    # Newest first: occurred_at non-increasing.
    stamps = [i["occurred_at"] for i in items]
    assert stamps == sorted(stamps, reverse=True)
    for item in items:
        assert "event_type" in item and "outcome" in item and "occurred_at" in item


async def test_audit_list_limit_validated(client):
    r = await client.get("/api/v1/audit?limit=201")
    assert r.status_code == 422


async def test_list_endpoints_do_not_mutate(client):
    # Reads must not create audit rows or tasks: count before/after.
    before = len((await client.get("/api/v1/audit?limit=200")).json()["items"])
    await client.get("/api/v1/sessions")
    await client.get("/api/v1/tasks")
    await client.get("/api/v1/audit")
    after = len((await client.get("/api/v1/audit?limit=200")).json()["items"])
    assert after == before


async def test_audit_row_shape_is_technical_and_redacted(client):
    # The audit view renders the event shape as-is; secrets are redacted at
    # write time, never shipped to the UI.
    sid = (await client.post("/api/v1/sessions")).json()["id"]
    await client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "minha chave é AKIAIOSFODNN7EXAMPLE"},
    )
    items = (await client.get("/api/v1/audit?limit=50")).json()["items"]
    blob = "\n".join(str(i) for i in items)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
