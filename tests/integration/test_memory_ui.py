"""Integration tests: F3.5 memory/context verification through the web shell.

Acceptance (PHASE3_PROPOSAL §7, §14.3): "Lembre-se de que eu gosto de café"
sent as a chat message persists a memory via the existing
MEMORY_WRITE -> MemoryService path (with secret scan), and the web UI
displays it read-only via GET /api/v1/memory, labeled as NOT-verified
(MemoryCtx.verified=False semantics: a past user statement, never a
measured fact).

The UI never mutates memory lifecycle: no confirm/revoke/supersede/delete
controls exist on the page, and no test below expects any.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from jarvis.api.main import create_app
from jarvis.config import Settings
from tests.conftest import StubNexusServer

# NOTE: the `db_url` and `stub_nexus` fixtures are provided by tests/conftest.py.

HARNESS = Path(__file__).with_name("web_shell_harness.js")
REPO = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = REPO / "src" / "jarvis" / "api" / "static" / "index.html"


@pytest.fixture()
def api_settings(db_url: str, stub_nexus: StubNexusServer) -> Settings:
    return Settings(
        database_url=db_url,
        nexus_base_url=stub_nexus.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture()
def app(api_settings: Settings) -> FastAPI:
    return create_app(api_settings)


@pytest.fixture()
def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create_session(client: httpx.AsyncClient) -> str:
    r = await client.post("/api/v1/sessions")
    assert r.status_code == 201
    body: dict[str, Any] = r.json()
    return str(body["id"])


# -- F3.5 acceptance: chat -> memory -> read-only UI contract ----------------


async def test_memory_write_via_chat_persists_and_lists(client: httpx.AsyncClient) -> None:
    # §14.3: "Lembre-se de que eu gosto de café" persists and is listed.
    sid = await _create_session(client)
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Lembre-se de que eu gosto de café"},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    assert "Guardei na memória" in str(body["message"])

    r = await client.get("/api/v1/memory")
    assert r.status_code == 200
    items: list[dict[str, Any]] = r.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert "café" in str(item["title"])
    assert item["kind"] == "preference"
    # Real lifecycle: a stored memory is ACTIVE; "unverified" is the
    # context-level labeling (MemoryCtx.verified is pinned False), which the
    # UI renders as "não verificada" (covered by the harness tests below).
    assert item["status"] == "active"
    # UI contract: the page needs exactly these fields to render.
    assert {"id", "kind", "title", "status"} <= set(item.keys())


async def test_memory_write_via_chat_with_secret_is_blocked(
    client: httpx.AsyncClient,
) -> None:
    # "sem segredo, sem bypass": the chat path runs the same secret scan as
    # the direct API path; nothing is persisted on block.
    sid = await _create_session(client)
    r = await client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Lembre-se de que minha chave é AKIAIOSFODNN7EXAMPLE"},
    )
    assert r.status_code == 422, r.text
    body: dict[str, Any] = r.json()
    assert body["error"]["message_key"] == "memory.secret_detected"

    r = await client.get("/api/v1/memory")
    items: list[dict[str, Any]] = r.json()["items"]
    assert items == []


async def test_memory_list_empty_contract(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/memory")
    assert r.status_code == 200
    assert r.json() == {"items": []}


# -- page behavior (Node harness, real inline script) -------------------------


def _run_page(scenario: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    scenario_file = tmp_path / "scenario.json"
    # Explicit UTF-8 on both ends: on Windows the default codec is cp1252,
    # while Node reads/writes UTF-8.
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
    node = shutil.which("node")
    assert node, "node is required for the web-shell behavioral tests"
    proc = subprocess.run(  # noqa: S603
        [node, str(HARNESS), str(INDEX_HTML), str(scenario_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    result: dict[str, Any] = json.loads(proc.stdout)
    assert "error" not in result, f"page script threw: {result['error']}"
    return result


def test_page_loads_memories_on_boot(tmp_path: Path) -> None:
    # The page fetches GET /api/v1/memory on boot and renders each item as
    # plain text (textContent), labeled as NOT-verified.
    result = _run_page(
        {
            "storedSession": None,
            "memories": [
                {"kind": "preference", "title": "eu gosto de café", "status": "pending"},
                {
                    "kind": "fact",
                    "title": "meu equipamento é o Quadro Geral",
                    "status": "confirmed",
                },
            ],
        },
        tmp_path,
    )
    calls = [c for c in result["fetchCalls"] if str(c["url"]).startswith("/api/v1/memory")]
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert len(result["memoryItems"]) == 2
    first = result["memoryItems"][0]
    assert first["title"] == "eu gosto de café"
    assert "preference" in first["meta"]
    assert "não verificada" in first["meta"]


def test_page_memories_empty_state(tmp_path: Path) -> None:
    result = _run_page({"storedSession": None, "memories": []}, tmp_path)
    assert len(result["memoryItems"]) == 1
    assert result["memoryItems"][0]["title"] == "Nenhuma memória ainda."


def test_page_memories_failure_is_non_fatal(tmp_path: Path) -> None:
    # A failing memory list must not break the chat: no throw, honest note.
    result = _run_page(
        {"storedSession": "sess-1", "history": [], "memories": {"__fail": True}},
        tmp_path,
    )
    assert len(result["memoryItems"]) == 1
    assert result["memoryItems"][0]["title"] == "Não foi possível carregar as memórias."
    assert result["stateLine"] != ""


def test_page_memories_preserve_unicode(tmp_path: Path) -> None:
    # F3.5 regression: non-ASCII memory titles must survive the whole chain
    # Python -> scenario.json -> Node -> stubbed DOM -> textContent exactly.
    sample = "Olá, JARVIS! äçõ € — 📌"
    result = _run_page(
        {
            "storedSession": None,
            "memories": [{"kind": "fact", "title": sample, "status": "pending"}],
        },
        tmp_path,
    )
    assert result["memoryItems"][0]["title"] == sample
