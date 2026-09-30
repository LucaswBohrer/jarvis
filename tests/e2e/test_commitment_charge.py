"""Slice 3 end-to-end: the in-conversation charge loop (test matrix T18).

Flow: chat creates a commitment -> it becomes due -> the next interaction
carries the deterministic "📌 Lembretes" block -> the user fulfills it in
chat -> the charge is gone. A second scenario proves an expired commitment
is never surfaced.

The NEXUS adapter and the LLM provider are EXPLODING stubs: any call
raises. If the commitment flow (create/fulfill/sweep/surfacing) ever
touched NEXUS, the LLM, or any external action, the test fails. Surfacing
is presentation inside the conversation only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jarvis.adapters.nexus.http import NexusAdapterConfig, NexusHttpAdapter
from jarvis.adapters.persistence.models import CommitmentRow
from jarvis.application.commitments import USER_EXPLICIT_ORIGIN, CommitmentService
from jarvis.application.memory_service import MemoryService
from jarvis.application.orchestrator import Orchestrator
from jarvis.domain.contracts.audit import AuditEventType
from jarvis.domain.contracts.commitments import CommitmentStatus
from jarvis.ports.clock import SystemClock
from jarvis.ports.llm import LLMProvider
from tests.conftest import new_session_id


class ExplodingNexus:
    """Every request raises (proves the commitment flow never reaches NEXUS)."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError("boom: NEXUS must not be called", request=request)


class ExplodingLLM:
    """Any structured generation attempt explodes."""

    name = "exploding"

    async def generate_structured(self, request, *, timeout_seconds: float):
        raise AssertionError("LLM must not be consulted by the commitment flow")

    async def health(self) -> bool:
        return True


@pytest.fixture()
def charge_setup(settings, repos, policy):
    exploding_nexus = ExplodingNexus()
    nexus = NexusHttpAdapter(
        NexusAdapterConfig(base_url="http://127.0.0.1:8000"),
        transport=httpx.MockTransport(exploding_nexus.handle_async_request),
    )
    llm: LLMProvider = ExplodingLLM()
    memory = MemoryService(
        db=repos["db"],
        memory=repos["memory"],
        audit=repos["audit"],
        policy=policy,
        clock=SystemClock(),
    )
    commitments = CommitmentService(
        db=repos["db"],
        commitments=repos["commitments"],
        memory=repos["memory"],
        audit=repos["audit"],
        meta=repos["service_meta"],
        clock=SystemClock(),
    )
    orch = Orchestrator(
        settings=settings,
        db=repos["db"],
        sessions=repos["sessions"],
        messages=repos["messages"],
        tasks=repos["tasks"],
        audit=repos["audit"],
        policy=policy,
        nexus=nexus,
        llm=llm,
        memory_service=memory,
        commitment_service=commitments,
    )
    return orch, exploding_nexus, commitments


async def test_charge_loop_create_surface_fulfill(charge_setup, repos):
    orch, nexus_stub, commitments = charge_setup
    sid = await new_session_id(repos["sessions"])

    # 1. The user creates the charge in chat. The confirmation carries no
    #    reminders block (the confirmation IS the charge); zero external calls.
    created = await orch.handle_message(sid, "me cobre de regar as plantas até amanhã")
    assert "regar as plantas" in created.message
    assert "📌 Lembretes" not in created.message
    assert nexus_stub.calls == 0

    # Make it due soon (deterministic, no waiting).
    items = await commitments.list_commitments(statuses=frozenset({CommitmentStatus.OPEN}))
    assert len(items) == 1
    target = items[0]
    async with commitments.db.session() as s, s.begin():
        row = await s.get(CommitmentRow, target.id)
        assert row is not None
        row.due_at = datetime.now(UTC) + timedelta(hours=2)

    # 2. Next interaction: a NEXUS-status ask. The exploding stub makes NEXUS
    #    UNAVAILABLE, so the deterministic fallback answers — with the
    #    reminders block appended.
    answer = await orch.handle_message(sid, "como está o nexus?")
    assert "📌 Lembretes:" in answer.message
    assert "regar as plantas" in answer.message
    # The status ask itself touched NEXUS (equipment + summary calls); the
    # commitment flow (create/sweep/surface) added zero.
    status_calls = nexus_stub.calls
    assert status_calls == 2

    # 3. The charge was stamped: no duplicate charge on the next interaction.
    answer2 = await orch.handle_message(sid, "como está o nexus?")
    assert "📌 Lembretes" not in answer2.message
    assert nexus_stub.calls == status_calls + 2  # only the status ask

    # 4. The user fulfills it in chat. Still zero commitment-flow external calls.
    done = await orch.handle_message(sid, "concluí regar as plantas")
    assert "concluído" in done.message
    assert nexus_stub.calls == status_calls + 2

    # 5. Gone: no more charges, and the audit trail is complete.
    answer3 = await orch.handle_message(sid, "como está o nexus?")
    assert "📌 Lembretes" not in answer3.message
    assert await commitments.list_commitments(statuses=frozenset({CommitmentStatus.OPEN})) == []
    created_types = [e.event_type for e in await repos["audit"].list_by_task(created.task_id)]
    assert AuditEventType.COMMITMENT_CREATED in created_types
    done_types = [e.event_type for e in await repos["audit"].list_by_task(done.task_id)]
    assert AuditEventType.COMMITMENT_FULFILLED in done_types
    surfaced_types = [e.event_type for e in await repos["audit"].list_by_task(answer.task_id)]
    assert AuditEventType.COMMITMENT_SURFACED in surfaced_types


async def test_expired_commitment_never_surfaces(charge_setup, repos):
    orch, _, commitments = charge_setup
    sid = await new_session_id(repos["sessions"])

    c = await commitments.create(
        title="coisa vencida",
        due_at=datetime.now(UTC) - timedelta(hours=2),
        origin=USER_EXPLICIT_ORIGIN,
        correlation_id="c",
    )
    # The lazy sweep at the start of the interaction expires it; an expired
    # (terminal) commitment is never surfaced as a charge.
    answer = await orch.handle_message(sid, "como está o nexus?")
    assert "📌 Lembretes" not in answer.message
    assert "coisa vencida" not in answer.message
    got = await commitments.get(c.id)
    assert got is not None and got.status is CommitmentStatus.EXPIRED
