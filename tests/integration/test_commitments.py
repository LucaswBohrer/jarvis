"""Slice 3 integration tests: commitment lifecycle, sweep, surfacing,
origin gate, audit trail, HTTP endpoints and chat intents (T13/T18)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jarvis.api.main import create_app
from jarvis.application.commitments import USER_EXPLICIT_ORIGIN
from jarvis.domain.contracts.audit import AuditEventType
from jarvis.domain.contracts.commitments import CommitmentStatus
from jarvis.domain.contracts.memory import MemoryKind, MemoryStatus, Provenance
from jarvis.domain.contracts.task import Intent, TaskInput, TaskKind, TaskRecord
from jarvis.domain.errors import ErrorCode, JarvisException
from tests.conftest import new_session_id


def _now() -> datetime:
    return datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


async def _task_id(repos) -> str:
    """A real task row, so audit FKs hold."""
    session_id = await new_session_id(repos["sessions"])
    task = TaskRecord(
        session_id=session_id,
        kind=TaskKind.COMMITMENT_CREATE,
        input=TaskInput(message_id="m1", utterance="test", intent=Intent.COMMITMENT_CREATE),
    )
    await repos["tasks"].create(task)
    return task.id


async def _audit_types(repos, task_id: str) -> list[str]:
    events = await repos["audit"].list_by_task(task_id)
    return [e.event_type.value for e in events]


# -- lifecycle ---------------------------------------------------------------


async def test_create_fulfill_cancel_lifecycle(commitment_service):
    now = _now()
    c = await commitment_service.create(
        title="regar as plantas",
        detail="as do quintal",
        due_at=now + timedelta(hours=2),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="cid-1",
    )
    assert c.status is CommitmentStatus.OPEN
    assert c.detail == "as do quintal"

    got = await commitment_service.get(c.id)
    assert got is not None and got.title == "regar as plantas"

    fulfilled = await commitment_service.fulfill(
        c.id, origin=USER_EXPLICIT_ORIGIN, correlation_id="cid-2"
    )
    assert fulfilled.status is CommitmentStatus.FULFILLED
    assert fulfilled.fulfilled_at is not None

    c2 = await commitment_service.create(
        title="pagar a conta", origin=USER_EXPLICIT_ORIGIN, correlation_id="cid-3"
    )
    cancelled = await commitment_service.cancel(
        c2.id, origin=USER_EXPLICIT_ORIGIN, correlation_id="cid-4"
    )
    assert cancelled.status is CommitmentStatus.CANCELLED


async def test_terminal_states_are_immutable(commitment_service):
    c = await commitment_service.create(
        title="x", origin=USER_EXPLICIT_ORIGIN, correlation_id="cid"
    )
    done = await commitment_service.fulfill(c.id, origin=USER_EXPLICIT_ORIGIN, correlation_id="cid")
    assert done.status is CommitmentStatus.FULFILLED
    for op in ("fulfill", "cancel"):
        with pytest.raises(JarvisException) as exc:
            await getattr(commitment_service, op)(
                c.id, origin=USER_EXPLICIT_ORIGIN, correlation_id="cid"
            )
        assert exc.value.error.code is ErrorCode.INPUT_INVALID
    # Missing id -> NOT_FOUND, not a silent no-op.
    with pytest.raises(JarvisException) as exc:
        await commitment_service.fulfill("nope", origin=USER_EXPLICIT_ORIGIN, correlation_id="cid")
    assert exc.value.error.code is ErrorCode.NOT_FOUND


async def test_origin_gate_rejects_non_user_origin(commitment_service):
    with pytest.raises(JarvisException) as exc:
        await commitment_service.create(title="x", origin="llm_suggestion", correlation_id="cid")
    assert exc.value.error.code is ErrorCode.POLICY_DENIED
    c = await commitment_service.create(
        title="x", origin=USER_EXPLICIT_ORIGIN, correlation_id="cid"
    )
    with pytest.raises(JarvisException) as exc:
        await commitment_service.fulfill(c.id, origin="nexus_event", correlation_id="cid")
    assert exc.value.error.code is ErrorCode.POLICY_DENIED


async def test_audit_trail_and_redaction(commitment_service, repos):
    tid = await _task_id(repos)
    c = await commitment_service.create(
        title="titulo secreto do usuario",
        due_at=_now() + timedelta(hours=1),
        origin=USER_EXPLICIT_ORIGIN,
        task_id=tid,
        correlation_id="cid",
    )
    await commitment_service.fulfill(
        c.id, origin=USER_EXPLICIT_ORIGIN, task_id=tid, correlation_id="cid"
    )
    types = await _audit_types(repos, tid)
    assert AuditEventType.COMMITMENT_CREATED.value in types
    assert AuditEventType.COMMITMENT_FULFILLED.value in types
    # Redaction: raw titles never reach audit summaries.
    events = await repos["audit"].list_by_task(tid)
    for e in events:
        assert "titulo secreto do usuario" not in (e.request_summary or "")
        assert "titulo secreto do usuario" not in (e.result_summary or "")


# -- sweep -------------------------------------------------------------------


async def test_sweep_expires_overdue_and_skips_null_due(commitment_service):
    past = _now() - timedelta(hours=1)
    overdue = await commitment_service.create(
        title="vencido", due_at=past, origin=USER_EXPLICIT_ORIGIN, correlation_id="c"
    )
    nodue = await commitment_service.create(
        title="sem prazo", origin=USER_EXPLICIT_ORIGIN, correlation_id="c"
    )
    future = await commitment_service.create(
        title="futuro",
        due_at=_now() + timedelta(days=2),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    result = await commitment_service.sweep(now=_now(), force=True, correlation_id="c")
    assert not result.cooldown_skipped
    assert result.expired_commitment_ids == [overdue.id]
    assert (await commitment_service.get(overdue.id)).status is CommitmentStatus.EXPIRED
    assert (await commitment_service.get(nodue.id)).status is CommitmentStatus.OPEN
    assert (await commitment_service.get(future.id)).status is CommitmentStatus.OPEN


async def test_sweep_is_idempotent(commitment_service, repos):
    tid = await _task_id(repos)
    past = _now() - timedelta(hours=1)
    c = await commitment_service.create(
        title="vencido",
        due_at=past,
        origin=USER_EXPLICIT_ORIGIN,
        task_id=tid,
        correlation_id="c",
    )
    first = await commitment_service.sweep(now=_now(), force=True, task_id=tid, correlation_id="c")
    assert first.expired_commitment_ids == [c.id]
    second = await commitment_service.sweep(
        now=_now(), force=True, task_id=tid, correlation_id="c2"
    )
    assert second.expired_commitment_ids == []
    # Exactly one commitment.expired audit event, no duplicates.
    types = await _audit_types(repos, tid)
    assert types.count(AuditEventType.COMMITMENT_EXPIRED.value) == 1


async def test_sweep_cooldown(commitment_service):
    r1 = await commitment_service.sweep(now=_now(), correlation_id="c")
    assert r1.cooldown_skipped is False  # first run: no watermark yet
    r2 = await commitment_service.sweep(now=_now(), correlation_id="c")
    assert r2.cooldown_skipped is True  # within the 1h window
    r3 = await commitment_service.sweep(now=_now() + timedelta(hours=2), correlation_id="c")
    assert r3.cooldown_skipped is False  # window elapsed
    r4 = await commitment_service.sweep(now=_now(), force=True, correlation_id="c")
    assert r4.cooldown_skipped is False  # force bypasses the cooldown


async def test_sweep_expires_memories_and_audits(commitment_service, memory_service, repos):
    from jarvis.domain.contracts.memory import MemoryItem

    tid = await _task_id(repos)
    # An already-expired memory row: the service refuses to CREATE one with
    # valid_until in the past, so insert at the repository level.
    expired_mem = MemoryItem(
        kind=MemoryKind.FACT,
        title="old fact",
        content="old fact content",
        confidence=1.0,
        provenance=Provenance.USER_EXPLICIT,
        status=MemoryStatus.ACTIVE,
        valid_until=_now() - timedelta(minutes=1),
    )
    await repos["memory"].create(expired_mem)
    fresh_mem = await memory_service.create(
        kind=MemoryKind.FACT,
        title="fresh fact",
        content="fresh fact content",
        valid_until=_now() + timedelta(days=1),
        correlation_id="c",
    )
    result = await commitment_service.sweep(now=_now(), force=True, task_id=tid, correlation_id="c")
    assert expired_mem.id in result.expired_memory_ids
    assert fresh_mem.id not in result.expired_memory_ids
    types = await _audit_types(repos, tid)
    assert AuditEventType.MEMORY_EXPIRED.value in types
    assert AuditEventType.COMMITMENT_SWEEP.value in types


# -- surfacing ---------------------------------------------------------------


async def test_due_for_surfacing_horizon_and_dedup(commitment_service):
    now = _now()
    due_soon = await commitment_service.create(
        title="em 2h",
        due_at=now + timedelta(hours=2),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    await commitment_service.create(
        title="em 3 dias",
        due_at=now + timedelta(days=3),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    await commitment_service.create(
        title="sem prazo", origin=USER_EXPLICIT_ORIGIN, correlation_id="c"
    )
    surfaced = await commitment_service.due_for_surfacing(now=now)
    assert [c.id for c in surfaced] == [due_soon.id]


async def test_mark_surfaced_stamps_and_audits(commitment_service, repos):
    tid = await _task_id(repos)
    now = _now()
    c = await commitment_service.create(
        title="em 2h",
        due_at=now + timedelta(hours=2),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    async with commitment_service._tx() as s:
        await commitment_service.mark_surfaced(
            [c.id],
            now=now,
            session_id=None,
            task_id=tid,
            correlation_id="c",
            sa_session=s,
        )
    got = await commitment_service.get(c.id)
    assert got is not None and got.last_surfaced_at is not None
    # 12h dedup: not surfaced again within the window...
    assert await commitment_service.due_for_surfacing(now=now + timedelta(hours=1)) == []
    # ...but resurfaces after 12h.
    later = await commitment_service.due_for_surfacing(now=now + timedelta(hours=13))
    assert [x.id for x in later] == [c.id]
    types = await _audit_types(repos, tid)
    assert AuditEventType.COMMITMENT_SURFACED.value in types


async def test_expired_commitments_are_never_surfaced(commitment_service):
    now = _now()
    c = await commitment_service.create(
        title="vai vencer",
        due_at=now - timedelta(minutes=1),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    await commitment_service.sweep(now=now, force=True, correlation_id="c")
    assert await commitment_service.due_for_surfacing(now=now) == []
    assert (await commitment_service.get(c.id)).status is CommitmentStatus.EXPIRED


# -- chat intents ------------------------------------------------------------


async def test_chat_create_commitment(orchestrator, repos):
    sid = await new_session_id(repos["sessions"])
    resp = await orchestrator.handle_message(sid, "me cobre de regar as plantas até amanhã")
    assert "regar as plantas" in resp.message
    # Due date rendered deterministically as dd/mm of "tomorrow".
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%d/%m")
    assert tomorrow in resp.message
    task = await repos["tasks"].get(resp.task_id)
    assert task is not None and task.kind is TaskKind.COMMITMENT_CREATE
    assert task.input.intent is Intent.COMMITMENT_CREATE
    items = await repos["commitments"].list_commitments(statuses=frozenset({CommitmentStatus.OPEN}))
    assert len(items) == 1 and items[0].title == "regar as plantas"
    assert items[0].due_at is not None


async def test_chat_create_without_due_date(orchestrator, repos):
    sid = await new_session_id(repos["sessions"])
    resp = await orchestrator.handle_message(sid, "me cobre de ler aquele artigo")
    assert "ler aquele artigo" in resp.message
    items = await repos["commitments"].list_commitments(statuses=frozenset({CommitmentStatus.OPEN}))
    assert len(items) == 1 and items[0].due_at is None


async def test_chat_create_empty_title_fails(orchestrator, repos):
    from jarvis.domain.errors import JarvisException

    sid = await new_session_id(repos["sessions"])
    with pytest.raises(JarvisException):
        await orchestrator.handle_message(sid, "me cobre")


async def test_chat_fulfill_matches_one(orchestrator, repos):
    sid = await new_session_id(repos["sessions"])
    await orchestrator.handle_message(sid, "me cobre de regar as plantas até amanhã")
    resp = await orchestrator.handle_message(sid, "concluí regar as plantas")
    assert "concluído" in resp.message
    task = await repos["tasks"].get(resp.task_id)
    assert task is not None and task.kind is TaskKind.COMMITMENT_FULFILL
    items = await repos["commitments"].list_commitments(statuses=frozenset({CommitmentStatus.OPEN}))
    assert items == []


async def test_chat_fulfill_no_match_fails(orchestrator, repos):
    from jarvis.domain.errors import JarvisException

    sid = await new_session_id(repos["sessions"])
    await orchestrator.handle_message(sid, "me cobre de regar as plantas")
    with pytest.raises(JarvisException) as exc:
        await orchestrator.handle_message(sid, "concluí lavar o carro")
    assert exc.value.error.code is ErrorCode.NOT_FOUND


async def test_chat_fulfill_ambiguous_fails(orchestrator, repos):
    from jarvis.domain.errors import JarvisException

    sid = await new_session_id(repos["sessions"])
    await orchestrator.handle_message(sid, "me cobre de regar as plantas")
    await orchestrator.handle_message(sid, "me cobre de regar o jardim")
    with pytest.raises(JarvisException) as exc:
        await orchestrator.handle_message(sid, "concluí regar")
    assert exc.value.error.code is ErrorCode.INPUT_INVALID


async def test_memory_write_wins_over_commitment_create(orchestrator, repos):
    # "lembre-se de me cobrar..." is a memory write, not a charge (D40).
    sid = await new_session_id(repos["sessions"])
    resp = await orchestrator.handle_message(sid, "lembre-se de me cobrar de estudar")
    task = await repos["tasks"].get(resp.task_id)
    assert task is not None and task.input.intent is Intent.MEMORY_WRITE
    assert await repos["commitments"].list_commitments() == []


async def test_lazy_sweep_runs_before_handling(orchestrator, repos, commitment_service):
    sid = await new_session_id(repos["sessions"])
    c = await commitment_service.create(
        title="vencido",
        due_at=_now() - timedelta(hours=1),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    # Any interaction triggers the lazy sweep; the overdue commitment expires.
    await orchestrator.handle_message(sid, "me cobre de algo novo")
    assert (await commitment_service.get(c.id)).status is CommitmentStatus.EXPIRED


# -- HTTP API ----------------------------------------------------------------


@pytest.fixture()
def api_settings(db_url, stub_nexus):
    from jarvis.config import Settings

    return Settings(
        database_url=db_url,
        nexus_base_url=stub_nexus.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture()
def api_client(api_settings):
    transport = httpx.ASGITransport(app=create_app(api_settings))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_commitment_endpoints(api_client):
    r = await api_client.post(
        "/api/v1/commitments",
        json={"title": "regar as plantas", "due_at": "2026-10-01T23:59:59+00:00"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["status"] == "open"

    r = await api_client.get("/api/v1/commitments", params={"status": "open"})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [cid]

    r = await api_client.post(f"/api/v1/commitments/{cid}/fulfill")
    assert r.status_code == 200 and r.json()["status"] == "fulfilled"

    # Terminal: fulfilling again -> 422, cancelling -> 422.
    r = await api_client.post(f"/api/v1/commitments/{cid}/fulfill")
    assert r.status_code == 422
    r = await api_client.post(f"/api/v1/commitments/{cid}/cancel")
    assert r.status_code == 422

    r = await api_client.post("/api/v1/commitments/does-not-exist/fulfill")
    assert r.status_code == 404

    r = await api_client.get("/api/v1/commitments", params={"status": "bogus"})
    assert r.status_code == 422


async def test_internal_sweep_route(api_client):
    r = await api_client.post(
        "/api/v1/commitments",
        json={"title": "vencido", "due_at": "2020-01-01T00:00:00+00:00"},
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    r = await api_client.post("/api/v1/internal/commitments/sweep")
    assert r.status_code == 200
    body = r.json()
    assert body["expired_commitment_ids"] == [cid]
    assert body["cooldown_skipped"] is False
    # Second call: cooldown (force defaults True on the route, so force=False
    # path is exercised through the service-level test instead).
    r = await api_client.post("/api/v1/internal/commitments/sweep", params={"force": "false"})
    assert r.json()["cooldown_skipped"] is True
