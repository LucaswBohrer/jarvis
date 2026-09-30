"""Integration tests: F3.3 conversation history (D1).

GET /api/v1/sessions/{session_id}/messages is strictly read-only: it exposes
the persisted conversation (role/content/created_at) in repository order so
the web shell can restore it after a reload. The page's bootstrap fetches it
and renders every bubble through textContent (never innerHTML).

Backend tests (T1-T5) run over httpx ASGITransport against a fresh migrated
DB. Frontend tests (T6-T11) execute the page's real inline <script> under
Node (tests/integration/web_shell_harness.js) with stubbed DOM and a
scripted fetch mock: they assert what the page actually does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jarvis.api.main import create_app
from jarvis.config import Settings
from jarvis.domain.contracts.session import MessageRole, UserMessage

# NOTE: the `db_url` and `stub_nexus` fixtures are provided by tests/conftest.py.

REPO = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = REPO / "src/jarvis/api/static/index.html"
HARNESS = Path(__file__).resolve().parent / "web_shell_harness.js"


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
    # raise_app_exceptions=False: emulate the production ASGI server.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create_session(client) -> str:
    r = await client.post("/api/v1/sessions")
    assert r.status_code == 201
    return r.json()["id"]


async def _seed_conversation(app, session_id: str, roles_contents: list[tuple[MessageRole, str]]):
    """Persist messages directly through the repository with ascending
    timestamps, so ordering is deterministic and independent of the
    orchestrator's timing."""
    state = app.state.jarvis
    base = datetime.now(UTC)
    for i, (role, content) in enumerate(roles_contents):
        await state.messages.add(
            UserMessage(
                session_id=session_id,
                role=role,
                content=content,
                created_at=base + timedelta(seconds=i),
            )
        )


# -- backend: GET /api/v1/sessions/{id}/messages ------------------------------


async def test_history_returns_messages_in_persisted_order(app, client):
    # T1 — user/assistant/user/assistant must come back in exactly that order.
    sid = await _create_session(client)
    await _seed_conversation(
        app,
        sid,
        [
            (MessageRole.USER, "primeira"),
            (MessageRole.ASSISTANT, "resposta um"),
            (MessageRole.USER, "segunda"),
            (MessageRole.ASSISTANT, "resposta dois"),
        ],
    )
    r = await client.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200
    items = r.json()["messages"]
    assert [m["role"] for m in items] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in items] == ["primeira", "resposta um", "segunda", "resposta dois"]
    created = [m["created_at"] for m in items]
    assert created == sorted(created)


async def test_history_unknown_session_404(client):
    # T2 — unknown session uses the existing JARVIS error contract.
    r = await client.get("/api/v1/sessions/does-not-exist/messages")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message_key"] == "session.not_found"


async def test_history_new_session_empty(client):
    # T3 — a session with no messages returns an empty list, not an error.
    sid = await _create_session(client)
    r = await client.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200
    assert r.json() == {"messages": []}


async def test_history_is_read_only(app, client):
    # T4 — GET must not change messages, sessions, tasks or anything else.
    sid = await _create_session(client)
    await _seed_conversation(app, sid, [(MessageRole.USER, "x"), (MessageRole.ASSISTANT, "y")])
    state = app.state.jarvis
    before_messages = [m.model_dump() for m in await state.messages.list_by_session(sid)]
    before_session = (await state.sessions.get(sid)).model_dump()
    before_tasks = [t.model_dump() for t in await state.tasks.list_non_terminal()]

    r = await client.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200

    after_messages = [m.model_dump() for m in await state.messages.list_by_session(sid)]
    after_session = (await state.sessions.get(sid)).model_dump()
    after_tasks = [t.model_dump() for t in await state.tasks.list_non_terminal()]
    assert after_messages == before_messages
    assert after_session == before_session
    assert after_tasks == before_tasks


async def test_history_contract_exposes_no_internals(app, client):
    # T5 — the DTO carries only what the UI needs: role, content, created_at.
    # No ORM rows, no internal ids, no idempotency keys, no secrets, no
    # infrastructure metadata.
    sid = await _create_session(client)
    await _seed_conversation(
        app, sid, [(MessageRole.USER, "segredo? não"), (MessageRole.ASSISTANT, "ok")]
    )
    r = await client.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"messages"}
    for item in body["messages"]:
        assert set(item.keys()) == {"role", "content", "created_at"}, item.keys()
        blob = json.dumps(item).lower()
        for token in ("idempotency", "task_id", "session_id", "secret", "password", "api_key"):
            assert token not in blob, f"leaked token in history payload: {token}"


# -- frontend: history restore in the real page script ------------------------

BUBBLE_KINDS = {"user", "assistant", "system", "error"}


def _run_page(scenario: dict, tmp_path) -> dict:
    """Execute the page's inline <script> under Node with the scripted fetch
    mock; return what the page did (fetch calls, rendered bubbles, UI state)."""
    scenario_file = tmp_path / "scenario.json"
    # Explicit UTF-8 on both ends: on Windows the default codec is cp1252,
    # while Node reads/writes UTF-8. Without this, "olá" round-trips as "olÃ¡".
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
    node = shutil.which("node")
    assert node, "node is required for the web-shell behavioral tests"
    # S603: fixed argv, no shell; the scenario file lives in pytest's tmp_path.
    proc = subprocess.run(  # noqa: S603
        [node, str(HARNESS), str(INDEX_HTML), str(scenario_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    result = json.loads(proc.stdout)
    assert "error" not in result, f"page script threw: {result['error']}"
    return result


def test_page_restores_history_on_boot(tmp_path):
    # T6 — with a stored session id, the page fetches its history and renders it.
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [
                {"role": "user", "content": "oi"},
                {"role": "assistant", "content": "olá"},
            ],
        },
        tmp_path,
    )
    history_calls = [
        c for c in result["fetchCalls"] if c["url"] == "/api/v1/sessions/sess-1/messages"
    ]
    assert len(history_calls) == 1 and history_calls[0]["method"] == "GET"
    assert [(b["kind"], b["text"]) for b in result["bubbles"]] == [
        ("user", "oi"),
        ("assistant", "olá"),
    ]
    assert result["stateLine"] == "pronta"


def test_page_preserves_history_order(tmp_path):
    # T7 — the page renders the received sequence verbatim (no re-sorting).
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [
                {"role": "user", "content": "A"},
                {"role": "assistant", "content": "B"},
                {"role": "user", "content": "C"},
            ],
        },
        tmp_path,
    )
    assert [b["text"] for b in result["bubbles"]] == ["A", "B", "C"]
    assert [b["kind"] for b in result["bubbles"]] == ["user", "assistant", "user"]


def test_page_empty_history_shows_empty_state(tmp_path):
    # T8 — [] renders a simple empty state, not a crash and not a persisted message.
    result = _run_page({"storedSession": "sess-1", "history": []}, tmp_path)
    assert [(b["kind"], b["text"]) for b in result["bubbles"]] == [
        ("system", "Nenhuma mensagem ainda.")
    ]
    assert result["stateLine"] == "pronta"


def test_page_history_xss_renders_as_text(tmp_path):
    # T9 — a message containing markup must appear as literal text; the
    # harness itself refuses to run if the page ever uses innerHTML.
    payload = "<script>alert(1)</script>"
    result = _run_page(
        {"storedSession": "sess-1", "history": [{"role": "user", "content": payload}]},
        tmp_path,
    )
    assert len(result["bubbles"]) == 1
    assert result["bubbles"][0]["text"] == payload
    assert result["bubbles"][0]["kind"] == "user"


def test_page_history_network_failure_is_non_fatal(tmp_path):
    # T10 — a failed history fetch must not break the page: no bubbles, UI
    # stays usable, and the stored session id is NOT silently discarded.
    result = _run_page({"storedSession": "sess-1", "history": {"__fail": True}}, tmp_path)
    assert result["bubbles"] == []
    assert result["stateLine"] == "pronta"
    assert result["storedSession"] == "sess-1"


def test_page_history_404_recovers_session_once(tmp_path):
    # T11 — a 404 on history triggers the existing session recovery: exactly
    # one fresh session is created (no loop), nothing is rendered, and the
    # new id is stored.
    result = _run_page(
        {
            "storedSession": "sess-gone",
            "createSessionId": "sess-fresh",
            "history": {"__status": 404},
        },
        tmp_path,
    )
    creates = [c for c in result["fetchCalls"] if c["url"] == "/api/v1/sessions"]
    assert len(creates) == 1 and creates[0]["method"] == "POST"
    assert result["storedSession"] == "sess-fresh"
    assert result["bubbles"] == []
    assert result["stateLine"] == "pronta"
    # No second history fetch: the recovery does not loop.
    history_calls = [c for c in result["fetchCalls"] if c["url"].endswith("/messages")]
    assert len(history_calls) == 1


def test_page_history_never_calls_nexus_or_uses_local_storage(tmp_path):
    # Boundary: the restore path adds no NEXUS surface and no localStorage.
    result = _run_page(
        {"storedSession": "sess-1", "history": [{"role": "user", "content": "x"}]}, tmp_path
    )
    for call in result["fetchCalls"]:
        assert ":8000" not in call["url"]
        assert "/api/v1/equipment" not in call["url"]
    assert all(b["kind"] in BUBBLE_KINDS for b in result["bubbles"])


def test_page_history_preserves_unicode_end_to_end(tmp_path):
    # T12 (F3.4.1 regression): non-ASCII text must survive the whole chain
    # Python -> scenario.json -> Node -> stubbed DOM -> textContent exactly.
    # On Windows the default codec (cp1252) used to corrupt this into mojibake
    # ("olá" observed as "olÃ¡").
    sample = "Olá, JARVIS! äçõ € — 📌"
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [
                {"role": "user", "content": sample},
                {"role": "assistant", "content": sample},
            ],
        },
        tmp_path,
    )
    assert [b["text"] for b in result["bubbles"]] == [sample, sample]
    assert [b["kind"] for b in result["bubbles"]] == ["user", "assistant"]
