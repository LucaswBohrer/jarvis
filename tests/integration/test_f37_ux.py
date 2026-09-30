"""Integration tests: F3.7 UX/UI hardening.

Behavioral tests for the hardened desktop shell, driven through the Node
harness against the REAL inline page script (no mocks of the page itself).

Covers: initial render (home/idle/orb), navigation across the 8 views,
sessions/tasks/activity/audit list rendering, the honest NEXUS status,
the send flow with real orb states (idle -> processing -> completed/error),
and the honest backend-down state (no invented states).

The cancel button (Esc / Cancelar) is wired to AbortController; aborting a
mid-flight POST is not simulated here because the stub fetch resolves
immediately — it is covered by manual verification.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

HARNESS = Path(__file__).with_name("web_shell_harness.js")
INDEX_HTML = (
    Path(__file__).resolve().parents[2] / "src" / "jarvis" / "api" / "static" / "index.html"
)


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


# -- initial render ------------------------------------------------------------


def test_initial_render_home_idle(tmp_path: Path) -> None:
    # Boot lands on home, orb idle, greeting present, backend live.
    result = _run_page({"storedSession": None, "memories": []}, tmp_path)
    assert result["views"] == ["home"]
    assert result["orbState"] == "idle"
    assert "Lucas" in (result["greeting"] or "")
    assert result["backendStatus"] == "Online (v0.1.0)"
    assert result["bootDone"] is True
    assert result["dataMotion"] == "full"


def test_health_failure_is_honest(tmp_path: Path) -> None:
    # Backend down: the UI says unavailable and the orb stays idle — it never
    # invents activity to look alive.
    result = _run_page({"storedSession": None, "healthFail": True, "memories": []}, tmp_path)
    assert result["backendStatus"] == "Unavailable"
    assert result["stateLine"] == "backend indisponível"
    assert result["orbState"] == "idle"
    assert result["views"] == ["home"]


# -- navigation ----------------------------------------------------------------


def test_navigation_switches_views(tmp_path: Path) -> None:
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "steps": [{"fire": ["nav-tasks", "click"]}],
        },
        tmp_path,
    )
    assert result["views"] == ["tasks"]
    tasks_calls = [c for c in result["fetchCalls"] if str(c["url"]).startswith("/api/v1/tasks")]
    assert len(tasks_calls) == 1


def test_navigation_audit_view(tmp_path: Path) -> None:
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "auditEvents": [
                {
                    "event_type": "turn.completed",
                    "outcome": "success",
                    "occurred_at": "2026-09-30T18:00:00+00:00",
                    "actor": "orchestrator",
                    "task_id": "task-1",
                    "session_id": "sess-1",
                }
            ],
            "steps": [{"fire": ["nav-audit", "click"]}],
        },
        tmp_path,
    )
    assert result["views"] == ["audit"]
    assert len(result["auditRows"]) == 1
    detail = result["auditRows"][0][1]
    assert "turn.completed" in detail
    assert "actor=orchestrator" in detail


def test_nexus_view_starts_unknown(tmp_path: Path) -> None:
    # F3.7 honesty rule: the NEXUS status is "not consulted" until a real
    # query happens — the UI never assumes availability.
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "steps": [{"fire": ["nav-nexus", "click"]}],
        },
        tmp_path,
    )
    assert result["views"] == ["nexus"]
    assert result["nexusStatus"] == "não consultado"
    posts = [c for c in result["fetchCalls"] if c["method"] == "POST"]
    assert not posts


# -- list views ----------------------------------------------------------------


def test_sessions_view_renders_list_and_switch(tmp_path: Path) -> None:
    sessions = [
        {"id": "sess-1", "status": "active", "created_at": "2026-09-30T18:00:00+00:00"},
        {"id": "sess-2", "status": "active", "created_at": "2026-09-30T17:00:00+00:00"},
    ]
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "sessionList": sessions,
            "steps": [
                {"fire": ["nav-sessions", "click"]},
                {"fireChild": ["session-list", 1, "click"]},
            ],
        },
        tmp_path,
    )
    assert result["views"] == ["home"]  # switching a session returns to home
    assert result["storedSession"] == "sess-2"
    rows = result["sessionRows"]
    assert len(rows) == 2
    flat = [" ".join(r) for r in rows]
    assert any("sess-1" in r and "atual" in r for r in flat)
    assert any("sess-2" in r for r in flat)


def test_tasks_view_renders_real_states(tmp_path: Path) -> None:
    tasks = [
        {
            "id": "task-1",
            "session_id": "sess-1",
            "kind": "nexus.status",
            "state": "running",
            "current_step": "call_nexus",
            "updated_at": "2026-09-30T18:00:00+00:00",
        },
        {
            "id": "task-2",
            "session_id": "sess-1",
            "kind": "nexus.status",
            "state": "completed",
            "updated_at": "2026-09-30T17:50:00+00:00",
        },
        {
            "id": "task-3",
            "session_id": "sess-1",
            "kind": "nexus.status",
            "state": "failed",
            "error_code": "NEXUS_UNAVAILABLE",
            "updated_at": "2026-09-30T17:40:00+00:00",
        },
    ]
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "taskList": tasks,
            "steps": [{"fire": ["nav-tasks", "click"]}],
        },
        tmp_path,
    )
    rows = result["taskRows"]
    assert len(rows) == 3
    flat = [" ".join(r) for r in rows]
    assert any("running" in r and "call_nexus" in r for r in flat)
    assert any("completed" in r for r in flat)
    assert any("failed" in r and "NEXUS_UNAVAILABLE" in r for r in flat)


def test_activity_view_renders_events(tmp_path: Path) -> None:
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "auditEvents": [
                {
                    "event_type": "turn.completed",
                    "outcome": "success",
                    "occurred_at": "2026-09-30T18:00:00+00:00",
                },
                {
                    "event_type": "policy.denied",
                    "outcome": "denied",
                    "occurred_at": "2026-09-30T18:01:00+00:00",
                },
            ],
            "steps": [{"fire": ["nav-activity", "click"]}],
        },
        tmp_path,
    )
    rows = result["activityRows"]
    assert len(rows) == 2
    flat = [" ".join(r) for r in rows]
    assert any("turn.completed" in r and "success" in r for r in flat)
    assert any("policy.denied" in r and "denied" in r for r in flat)


# -- send flow with honest orb states -------------------------------------------


def test_send_flow_orb_completed(tmp_path: Path) -> None:
    # A real send moves the orb to the completed state; the POST carries the
    # exact user text and the assistant bubble renders the real payload.
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {
                "status": "ok",
                "message": "resposta real do backend",
                "source": "fake",
                "task_id": "task-42",
            },
            "steps": [
                {"set": ["input", "olá jarvis"]},
                {"fire": ["composer", "submit"]},
            ],
        },
        tmp_path,
    )
    posts = [c for c in result["fetchCalls"] if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["body"]["content"] == "olá jarvis"
    # Boot with empty history renders the "no messages yet" system note first.
    kinds = [b["kind"] for b in result["bubbles"]]
    assert kinds == ["system", "user", "assistant"]
    assert result["bubbles"][1]["text"] == "olá jarvis"
    assert result["bubbles"][2]["text"] == "resposta real do backend"
    assert result["orbState"] == "complete"
    assert result["stateLine"] == "ok"
    assert result["panelTask"] == "task-42"


def test_send_error_orb_error(tmp_path: Path) -> None:
    # A failed send surfaces the real error and the orb shows the error state.
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {"__status": 422, "__detail": "INTENT_UNSUPPORTED"},
            "steps": [
                {"set": ["input", "blá blá blá"]},
                {"fire": ["input", "keydown", {"key": "Enter", "shiftKey": False}]},
            ],
        },
        tmp_path,
    )
    kinds = [b["kind"] for b in result["bubbles"]]
    assert kinds == ["system", "user", "error"]
    assert "INTENT_UNSUPPORTED" in result["bubbles"][2]["text"]
    assert result["orbState"] == "error"
    assert result["stateLine"].startswith("erro:")


def test_nexus_query_honest_down(tmp_path: Path) -> None:
    # "Consultar agora" performs a real turn; when the backend reports the
    # NEXUS as unavailable, the NEXUS view reflects exactly that.
    unavailable_msg = "Não consigo consultar o NEXUS neste momento. A API local está indisponível."
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {
                "status": "unavailable",
                "message": unavailable_msg,
                "source": "orchestrator",
                "task_id": "task-9",
            },
            "steps": [{"fire": ["nexus-query", "click"]}],
        },
        tmp_path,
    )
    posts = [c for c in result["fetchCalls"] if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["body"]["content"] == "Como está o NEXUS?"
    assert result["views"] == ["home"]
    assert result["nexusStatus"] == "indisponível"
    assistant = [b for b in result["bubbles"] if b["kind"] == "assistant"]
    assert len(assistant) == 1
    assert unavailable_msg in assistant[0]["text"]
