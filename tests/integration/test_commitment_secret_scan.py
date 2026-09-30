"""F1 hardening: secret scanning on the commitment write path (T1–T9).

Finding F1 (PHASE2_FINAL_AUDIT.md): CommitmentService.create accepted
title/detail without scan_for_secrets, letting a secret be persisted in
SQLite, re-surfaced in the conversation and potentially sent to the
external LLM provider through the conversation tail.

Guarantee after the fix: no title/detail carrying a secret pattern ever
reaches persistence, so the Commitment -> SQLite -> tail -> Context ->
LLM chain cannot become a secret-exfiltration path. The block audits
commitment.write_blocked (categories only, never values) in its own
transaction, surviving the rollback of the persistence transaction that
never happened (D31 property).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from jarvis.api.main import create_app
from jarvis.application.commitments import USER_EXPLICIT_ORIGIN, CommitmentService
from jarvis.application.context_builder import ContextBuilder, ContextBuildInput
from jarvis.config import Settings
from jarvis.domain.contracts.audit import AuditEventType
from jarvis.domain.contracts.commitments import CommitmentStatus
from jarvis.domain.contracts.memory import MemoryItem, MemoryKind, Provenance
from jarvis.domain.contracts.task import Intent, TaskInput, TaskKind, TaskRecord
from jarvis.domain.errors import ErrorCode, JarvisException
from tests.conftest import new_session_id

CID = "test-cid-commitment-scan"

# Synthetic test vector, not a credential (built by concatenation so no
# literal key-like string appears in source).  # noqa: S105
SECRET = "sk-" + "A" * 24


def _now():
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


async def _events(repos, task_id):
    return await repos["audit"].list_by_task(task_id)


def _blocked(events):
    return [e for e in events if e.event_type is AuditEventType.COMMITMENT_WRITE_BLOCKED]


async def _create_blocked(
    commitment_service, repos, *, title, detail=None, task_id=None, origin=USER_EXPLICIT_ORIGIN
):
    tid = task_id or await _task_id(repos)
    with pytest.raises(JarvisException) as exc:
        await commitment_service.create(
            title=title,
            detail=detail,
            origin=origin,
            task_id=tid,
            correlation_id=CID,
        )
    err = exc.value.error
    assert err.code is ErrorCode.COMMITMENT_SECRET_DETECTED
    assert err.user_message_key == "commitment.secret_detected"
    return tid


async def _assert_nothing_persisted(commitment_service, repos, secret, tid):
    """Zero rows, zero retrievable traces, zero secret in the audit trail."""
    assert await commitment_service.list_commitments() == []
    assert await commitment_service.due_for_surfacing(now=_now()) == []
    assert await repos["audit"].list_by_task(tid) != []
    events = await _events(repos, tid)
    blocked = _blocked(events)
    assert blocked, "expected a commitment.write_blocked audit event"
    assert AuditEventType.COMMITMENT_CREATED.value not in [e.event_type.value for e in events]
    for e in blocked:
        blob = (e.request_summary or "") + (e.result_summary or "")
        assert secret not in blob, "secret value leaked into the audit trail"
        assert "openai_api_key" in blob  # category is recorded, value is not


# -- T1: secret in title -------------------------------------------------------


async def test_t1_secret_in_title_blocked(commitment_service, repos):
    tid = await _create_blocked(
        commitment_service, repos, title=f"pagar a conta com a chave {SECRET}"
    )
    await _assert_nothing_persisted(commitment_service, repos, SECRET, tid)


# -- T2: secret in detail ------------------------------------------------------


async def test_t2_secret_in_detail_blocked(commitment_service, repos):
    tid = await _create_blocked(
        commitment_service,
        repos,
        title="estudar física 3",
        detail=f"usar a API com {SECRET} no script",
    )
    await _assert_nothing_persisted(commitment_service, repos, SECRET, tid)


# -- T3: secret in title + detail ----------------------------------------------


async def test_t3_secret_in_title_and_detail_blocked(commitment_service, repos):
    tid = await _create_blocked(
        commitment_service,
        repos,
        title=f"chave {SECRET} para o deploy",
        detail=f"token alternativo {SECRET}",
    )
    await _assert_nothing_persisted(commitment_service, repos, SECRET, tid)


# -- T4: clean commitment persists, lifecycle intact ----------------------------


async def test_t4_clean_commitment_persists_and_lifecycle_intact(commitment_service, repos):
    tid = await _task_id(repos)
    item = await commitment_service.create(
        title="estudar física 3",
        detail="capítulo 4",
        due_at=_now() + timedelta(days=1),
        origin=USER_EXPLICIT_ORIGIN,
        task_id=tid,
        correlation_id=CID,
    )
    assert item.status is CommitmentStatus.OPEN
    listed = await commitment_service.list_commitments()
    assert [c.id for c in listed] == [item.id]
    events = await _events(repos, tid)
    assert AuditEventType.COMMITMENT_CREATED in [e.event_type for e in events]
    assert not _blocked(events)

    # Lifecycle is untouched by the hardening: fulfill still works.
    done = await commitment_service.fulfill(
        item.id, origin=USER_EXPLICIT_ORIGIN, task_id=tid, correlation_id=CID
    )
    assert done.status is CommitmentStatus.FULFILLED


# -- T5: block audit survives rollback ------------------------------------------


async def test_t5_block_audit_survives_rollback(commitment_service, repos):
    tid = await _create_blocked(commitment_service, repos, title=f"cobrar com {SECRET}")

    # Force a rollback of an unrelated outer transaction: the blocked audit
    # was committed in its own transaction, so it must survive.
    db = repos["db"]
    junk = MemoryItem(
        kind=MemoryKind.FACT,
        title="junk",
        content="junk",
        provenance=Provenance.USER_EXPLICIT,
        confidence=1.0,
    )
    try:
        async with db.session() as s, s.begin():
            await repos["memory"].create(junk, sa_session=s)
            raise RuntimeError("forced rollback")
    except RuntimeError:
        pass

    # The rolled-back junk is gone...
    assert await repos["memory"].get(junk.id) is None
    # ...but the block audit remains durable.
    blocked = _blocked(await _events(repos, tid))
    assert blocked, "commitment.write_blocked lost after rollback (D31 regression)"
    assert blocked[0].outcome.value == "denied"


# -- T6: provider boundary -------------------------------------------------------


async def test_t6_blocked_secret_never_reaches_provider(commitment_service, repos):
    """Adversarial: the only commitment routes into the LLM-bound snapshot
    are due_for_surfacing() and list_commitments() — both read persisted
    state. A blocked write persists nothing, so the provider-bound
    ContextSnapshot cannot contain the secret."""
    # The commitment would have been due 1h ago (surfacable if persisted).
    tid = await _task_id(repos)
    with pytest.raises(JarvisException):
        await commitment_service.create(
            title=f"me cobre de pagar com {SECRET}",
            detail=f"detalhe {SECRET}",
            due_at=_now() - timedelta(hours=1),
            origin=USER_EXPLICIT_ORIGIN,
            task_id=tid,
            correlation_id=CID,
        )

    due = await commitment_service.due_for_surfacing(now=_now())
    assert due == [], "blocked commitment must never be surfacable"

    builder = ContextBuilder()
    snapshot = builder.build(
        ContextBuildInput(verified=None, commitments_due=tuple(due)),
        task_id=tid,
        session_id="s1",
    )
    blob = snapshot.model_dump_json()
    assert SECRET not in blob
    assert snapshot.commitments_due == []


# -- T7: FTS ----------------------------------------------------------------------


async def test_t7_blocked_secret_not_in_fts(commitment_service, repos):
    """The FTS5 index covers only memory_items (migration 0004); commitments
    are never indexed. The guarantee is non-persistence: nothing stored
    means nothing indexed, nothing retrievable."""
    await _create_blocked(commitment_service, repos, title=f"chave {SECRET}")

    db = repos["db"]
    async with db.session() as s:
        fts_rows = (await s.execute(text("SELECT COUNT(*) FROM memory_fts"))).scalar()
        commitment_rows = (await s.execute(text("SELECT COUNT(*) FROM commitments"))).scalar()
    assert fts_rows == 0
    assert commitment_rows == 0


# -- T8: API cannot bypass the scanner ---------------------------------------------


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
def api_client(api_settings):
    transport = httpx.ASGITransport(app=create_app(api_settings))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_t8_api_post_with_secret_is_rejected(api_client):
    r = await api_client.post(
        "/api/v1/commitments",
        json={"title": f"pagar com {SECRET}", "detail": "detalhe limpo"},
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == ErrorCode.COMMITMENT_SECRET_DETECTED.value
    assert body["message_key"] == "commitment.secret_detected"
    assert SECRET not in r.text

    # Nothing was persisted through the HTTP path either.
    r = await api_client.get("/api/v1/commitments", params={"status": "open"})
    assert r.status_code == 200
    assert r.json()["items"] == []


# -- T9: scan runs regardless of origin -----------------------------------------------


async def test_t9_scan_runs_regardless_of_origin(commitment_service, repos):
    tid = await _task_id(repos)
    # Invalid origin + secret: the secret scan fires first, not the origin gate.
    with pytest.raises(JarvisException) as exc:
        await commitment_service.create(
            title=f"chave {SECRET}",
            origin="llm_output",
            task_id=tid,
            correlation_id=CID,
        )
    assert exc.value.error.code is ErrorCode.COMMITMENT_SECRET_DETECTED

    # The origin gate itself is unchanged: invalid origin + clean text.
    with pytest.raises(JarvisException) as exc2:
        await commitment_service.create(
            title="texto limpo",
            origin="llm_output",
            task_id=tid,
            correlation_id=CID,
        )
    assert exc2.value.error.code is ErrorCode.POLICY_DENIED


# -- security invariants ------------------------------------------------------------


async def test_invariants_memory_and_commitment_secrets_blocked(
    commitment_service, memory_service, repos
):
    """memory secret -> blocked; commitment secret -> blocked; neither can
    authorize tools (commitments never enter policy/tool paths by D40;
    memories are data, never instructions)."""
    tid = await _task_id(repos)
    with pytest.raises(JarvisException) as exc:
        await memory_service.create(
            kind=MemoryKind.FACT,
            title="t",
            content=f"chave {SECRET}",
            task_id=tid,
            correlation_id=CID,
        )
    assert exc.value.error.code is ErrorCode.MEMORY_SECRET_DETECTED

    with pytest.raises(JarvisException) as exc2:
        await commitment_service.create(
            title=f"chave {SECRET}",
            origin=USER_EXPLICIT_ORIGIN,
            task_id=tid,
            correlation_id=CID,
        )
    assert exc2.value.error.code is ErrorCode.COMMITMENT_SECRET_DETECTED

    assert isinstance(commitment_service, CommitmentService)
    # No commitment path feeds policy or tool execution: create/fulfill/cancel
    # only touch the commitments table + audit (D40). Verified by code
    # inspection; this test pins the scan behavior both share.
    assert await commitment_service.list_commitments() == []
