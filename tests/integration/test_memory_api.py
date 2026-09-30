"""T16 — API: memory endpoints over HTTP.

Driven over httpx ASGITransport against the real app: the full lifecycle
(chat create -> list -> get -> supersede -> revoke -> delete -> purge),
the secret-content 422, export, and 404s.
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


async def _session_id(client) -> str:
    return (await client.post("/api/v1/sessions", json={})).json()["id"]


async def _remember(client, sid, text):
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages", json={"content": f"Lembre-se: {text}"}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _items(r) -> list:
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_full_lifecycle_via_http(client):
    sid = await _session_id(client)

    # Create through the chat pipeline (explicit user command).
    body = await _remember(client, sid, "meu time é o Grêmio")
    assert body["status"] == "normal"

    # List finds it.
    items = _items(await client.get("/api/v1/memory"))
    assert len(items) == 1
    item_id = items[0]["id"]
    assert items[0]["status"] == "active"

    # FTS search finds it by content.
    items = _items(await client.get("/api/v1/memory", params={"q": "Grêmio"}))
    assert len(items) == 1

    # Get by id.
    r = await client.get(f"/api/v1/memory/{item_id}")
    assert r.status_code == 200 and r.json()["title"] is not None

    # Supersede (201).
    r = await client.post(
        f"/api/v1/memory/{item_id}/supersede",
        json={"content": "meu time é o Internacional"},
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    assert new_id != item_id

    # The predecessor is now terminal (superseded).
    r = await client.get(f"/api/v1/memory/{item_id}")
    assert r.json()["status"] == "superseded"

    # Revoke the successor.
    r = await client.post(f"/api/v1/memory/{new_id}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked"

    # Soft delete a fresh item (revoke is terminal -> cannot delete it).
    await _remember(client, sid, "nota temporária para apagar")
    tmp_id = _items(await client.get("/api/v1/memory", params={"q": "temporária"}))[0]["id"]
    r = await client.delete(f"/api/v1/memory/{tmp_id}")
    assert r.status_code == 200 and r.json()["status"] == "deleted"

    # Purge without delete first is rejected (two-step).
    r = await client.delete(f"/api/v1/memory/{new_id}", params={"purge": "true"})
    assert r.status_code == 422, r.text

    # Purge the deleted one: 204, then gone.
    r = await client.delete(f"/api/v1/memory/{tmp_id}", params={"purge": "true"})
    assert r.status_code == 204
    r = await client.get(f"/api/v1/memory/{tmp_id}")
    assert r.status_code == 404


async def test_secret_content_rejected_with_422(client):
    sid = await _session_id(client)
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Lembre-se: minha chave é sk-abcdefghijklmnopqrstuvwx"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "MEMORY_SECRET_DETECTED"


async def test_export_returns_json_array(client):
    sid = await _session_id(client)
    await _remember(client, sid, "item de exportação")
    r = await client.get("/api/v1/memory/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 2  # memory contract schema, not alembic rev
    assert isinstance(body["items"], list) and len(body["items"]) >= 1
    assert all("content" in i for i in body["items"])


async def test_memory_read_via_chat(client):
    sid = await _session_id(client)
    await _remember(client, sid, "minha cor favorita é azul")
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "O que você sabe sobre minha cor favorita?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "normal"
    assert "azul" in body["message"]


async def test_unknown_id_404s(client):
    r = await client.get("/api/v1/memory/nao-existe")
    assert r.status_code == 404
    r = await client.post("/api/v1/memory/nao-existe/revoke")
    assert r.status_code == 404
    r = await client.delete("/api/v1/memory/nao-existe")
    assert r.status_code == 404
