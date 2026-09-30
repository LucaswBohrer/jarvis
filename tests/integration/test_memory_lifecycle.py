"""T5 — Memory lifecycle: create/supersede/revoke/confirm/delete/purge.

Covers the full item state machine, provenance inheritance on supersede,
and the physical-delete semantics of purge.
"""

from __future__ import annotations

import pytest

from jarvis.domain.contracts.memory import (
    MemoryKind,
    MemoryStatus,
    Provenance,
    Sensitivity,
)
from jarvis.domain.errors import ErrorCode, JarvisException

CID = "test-correlation"


async def _create(svc, **over):
    args = {
        "kind": MemoryKind.FACT,
        "title": "Gosto de café sem açúcar",
        "content": "Lucas prefere café sem açúcar.",
        "correlation_id": CID,
    }
    args.update(over)
    return await svc.create(**args)


async def test_create_defaults(memory_service):
    item = await _create(memory_service)
    # An explicit user command is trusted: the item is born active.
    # PENDING exists for future flows (imports); the contract keeps it.
    assert item.status is MemoryStatus.ACTIVE
    assert item.provenance is Provenance.USER_EXPLICIT
    assert item.sensitivity is Sensitivity.STANDARD
    assert item.confidence == 1.0
    assert item.superseded_by is None


async def test_confirm_pending_to_active(memory_service, repos):
    # PENDING items can only arrive via direct insert for now (future:
    # imports). Confirming moves them to active.
    from jarvis.domain.contracts.memory import MemoryItem

    pending = MemoryItem(
        kind=MemoryKind.FACT,
        title="t",
        content="c",
        provenance=Provenance.USER_EXPLICIT,
        confidence=1.0,
        status=MemoryStatus.PENDING,
    )
    await repos["memory"].create(pending)
    confirmed = await memory_service.confirm(pending.id, correlation_id=CID)
    assert confirmed.status is MemoryStatus.ACTIVE


async def test_confirm_non_pending_fails(memory_service):
    item = await _create(memory_service)
    await memory_service.revoke(item.id, correlation_id=CID)
    with pytest.raises(JarvisException) as exc:
        await memory_service.confirm(item.id, correlation_id=CID)
    assert exc.value.error.code is ErrorCode.INPUT_INVALID


async def test_supersede_inherits_kind_and_provenance(memory_service):
    old = await _create(memory_service, kind=MemoryKind.PREFERENCE)
    new = await memory_service.supersede(
        old.id, content="Lucas agora prefere chá.", correlation_id=CID
    )
    assert new.kind is MemoryKind.PREFERENCE
    assert new.provenance is Provenance.USER_EXPLICIT
    assert new.status is MemoryStatus.ACTIVE
    assert new.superseded_by is None

    fetched_old = await memory_service.get_authorized(old.id, correlation_id=CID)
    assert fetched_old is not None
    assert fetched_old.status is MemoryStatus.SUPERSEDED
    assert fetched_old.superseded_by == new.id


async def test_supersede_terminal_target_fails(memory_service):
    item = await _create(memory_service)
    await memory_service.revoke(item.id, correlation_id=CID)
    with pytest.raises(JarvisException) as exc:
        await memory_service.supersede(item.id, content="x", correlation_id=CID)
    assert exc.value.error.code is ErrorCode.INPUT_INVALID


async def test_revoke_is_terminal(memory_service):
    item = await _create(memory_service)
    revoked = await memory_service.revoke(item.id, correlation_id=CID)
    assert revoked.status is MemoryStatus.REVOKED
    # Terminal statuses can never leave.
    with pytest.raises(JarvisException):
        await memory_service.confirm(item.id, correlation_id=CID)


async def test_delete_is_soft(memory_service):
    item = await _create(memory_service)
    deleted = await memory_service.delete(item.id, correlation_id=CID)
    assert deleted.status is MemoryStatus.DELETED
    # Still in the DB (tombstone), retrievable by id.
    fetched = await memory_service.get_authorized(item.id, correlation_id=CID)
    assert fetched is not None and fetched.status is MemoryStatus.DELETED


async def test_purge_is_physical(memory_service):
    item = await _create(memory_service)
    await memory_service.delete(item.id, correlation_id=CID)
    await memory_service.purge(item.id, correlation_id=CID)
    assert await memory_service.get_authorized(item.id, correlation_id=CID) is None


async def test_purge_requires_delete_first(memory_service):
    item = await _create(memory_service)
    with pytest.raises(JarvisException) as exc:
        await memory_service.purge(item.id, correlation_id=CID)
    assert exc.value.error.code is ErrorCode.INPUT_INVALID


async def test_purge_clears_superseded_by_references(memory_service):
    old = await _create(memory_service)
    new = await memory_service.supersede(old.id, content="novo", correlation_id=CID)
    # old.superseded_by -> new.id. Deleting + purging the NEW item must null
    # the survivor's link, never leave it dangling.
    await memory_service.delete(new.id, correlation_id=CID)
    await memory_service.purge(new.id, correlation_id=CID)
    survivor = await memory_service.get_authorized(old.id, correlation_id=CID)
    assert survivor is not None
    assert survivor.superseded_by is None


async def test_unknown_target_fails(memory_service):
    with pytest.raises(JarvisException) as exc:
        await memory_service.revoke("no-such-id", correlation_id=CID)
    assert exc.value.error.code is ErrorCode.NOT_FOUND
    with pytest.raises(JarvisException):
        await memory_service.purge("no-such-id", correlation_id=CID)


async def test_secret_write_blocked_and_not_persisted(memory_service):
    with pytest.raises(JarvisException) as exc:
        await _create(
            memory_service,
            title="minha chave",
            content="a chave é sk-abcdefghijklmnopqrstuvwx, guarde bem",
        )
    assert exc.value.error.code is ErrorCode.MEMORY_SECRET_DETECTED
    items = await memory_service.list_authorized(correlation_id=CID)
    assert items == []
