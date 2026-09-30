"""Memory application service (Phase 2, Slice 1).

Every write op follows the same pipeline, in this order:
1. Contract validation (MemoryWriteArgs) — malformed args never reach policy.
2. Policy decision via ToolRequest(memory.write) — BEFORE any persistence.
3. Secret scan — BEFORE persistence. A hit denies the write; nothing is
   stored and only the pattern category (never the value) is audited.
4. Persistence + audit in a single transaction.
NEVER in any other order.

Reads are read-only by construction. The `read`/`export` entry points run
the policy check first (memory.read); `retrieve` is the raw read-only port
for callers that already authorized.

Memory is DATA, never instruction: nothing this service returns is ever fed
back as a policy rule, a tool call, or a CanonicalFact.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import AwareDatetime, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.persistence.database import Database
from ..adapters.persistence.repositories import SqlAuditRepository, SqlMemoryRepository
from ..domain.contracts.audit import AuditActor, AuditEventType, AuditLog, AuditOutcome
from ..domain.contracts.common import new_id
from ..domain.contracts.memory import (
    TERMINAL_STATUSES,
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryReadResult,
    MemoryStatus,
    MemoryWriteOp,
    Provenance,
    Sensitivity,
)
from ..domain.contracts.policy import PolicyDecision, PolicyEffect
from ..domain.contracts.tool import (
    Capability,
    MemoryReadArgs,
    MemoryWriteArgs,
    ToolArguments,
    ToolName,
    ToolRequest,
)
from ..domain.errors import ErrorCategory, ErrorCode, ErrorSource, JarvisError, JarvisException
from ..ports.clock import Clock, SystemClock
from ..security.memory_safety import scan_for_secrets
from ..security.policy import PolicyEngine

_WRITEABLE_FROM = frozenset({MemoryStatus.ACTIVE, MemoryStatus.PENDING})

_EXPORT_LIMIT = 10_000


def _error(
    code: ErrorCode,
    user_message_key: str,
    correlation_id: str,
    *,
    category: ErrorCategory = ErrorCategory.INTERNAL,
    source: ErrorSource = ErrorSource.SYSTEM,
) -> JarvisError:
    return JarvisError(
        code=code,
        category=category,
        user_message_key=user_message_key,
        retryable=False,
        source=source,
        correlation_id=correlation_id,
    )


def _request_summary(request: ToolRequest) -> str:
    """Audit-safe request summary. NEVER includes memory title/content/query
    text — raw content must not flow into the audit log (secret hygiene)."""
    args: ToolArguments = request.arguments
    if isinstance(args, MemoryWriteArgs):
        kind = args.kind.value if args.kind is not None else "-"
        return (
            f"memory.write op={args.op.value} kind={kind} "
            f"target={args.target_id or '-'} origin={args.origin}"
        )
    if isinstance(args, MemoryReadArgs):
        query = args.query
        return f"memory.read query_len={len(query.query)} limit={query.limit}"
    return f"{request.tool_name.value} args={type(args).__name__}"


@dataclass
class MemoryService:
    db: Database
    memory: SqlMemoryRepository
    audit: SqlAuditRepository
    policy: PolicyEngine
    clock: Clock = field(default_factory=SystemClock)

    # -- internal pipeline --------------------------------------------------------

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self.db.session() as session, session.begin():
            yield session

    def _args(self, op: MemoryWriteOp, correlation_id: str, **kwargs: Any) -> MemoryWriteArgs:
        try:
            return MemoryWriteArgs(op=op, **kwargs)
        except ValidationError as exc:
            raise JarvisException(
                _error(ErrorCode.INPUT_INVALID, "memory.invalid_args", correlation_id)
            ) from exc

    def _request(
        self,
        tool_name: ToolName,
        capability: Capability,
        args: ToolArguments,
        task_id: str | None,
        correlation_id: str,
    ) -> ToolRequest:
        now = self.clock.now()
        # The ToolRequest needs a task_id; endpoint calls have none, so a
        # synthetic one is used for the policy evaluation only. Audit rows
        # keep task_id=None (audit_logs.task_id is a FK to real tasks).
        return ToolRequest(
            task_id=task_id or f"memory-{new_id()}",
            correlation_id=correlation_id,
            tool_name=tool_name,
            capability=capability,
            arguments=args,
            requested_at=now,
            deadline_at=now + timedelta(seconds=2),
        )

    async def _authorize(
        self,
        request: ToolRequest,
        *,
        session_id: str | None,
        task_id: str | None,
        correlation_id: str,
    ) -> PolicyDecision:
        """Policy gate. The decision audit is committed in its own transaction
        so a DENY leaves a durable trail even though the write transaction
        never happens (or rolls back)."""
        decision = self.policy.decide(request, method="INTERNAL", url="internal://local-memory/")
        allowed = decision.decision is PolicyEffect.ALLOW
        async with self._tx() as s:
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.POLICY_DECIDED,
                    actor=AuditActor.LOCAL_USER,
                    capability=request.capability.value,
                    tool_name=request.tool_name.value,
                    decision=decision.decision,
                    outcome=AuditOutcome.SUCCESS if allowed else AuditOutcome.DENIED,
                    request_summary=_request_summary(request),
                    result_summary=(
                        f"rule={decision.matched_rule_id} reason={decision.reason_code.value}"
                    ),
                ),
                sa_session=s,
            )
        if not allowed:
            raise JarvisException(
                _error(
                    ErrorCode.POLICY_DENIED,
                    "memory.policy_denied",
                    correlation_id,
                    category=ErrorCategory.POLICY,
                )
            )
        return decision

    async def _scan_or_block(
        self,
        text: str,
        *,
        op_label: str,
        kind: MemoryKind | None,
        session_id: str | None,
        task_id: str | None,
        correlation_id: str,
    ) -> None:
        """Secret scan. Pure scan first; a hit is audited in its own
        transaction (categories only, never values) so the block survives
        the rollback of the write that never happened."""
        hits = scan_for_secrets(text)
        if not hits:
            return
        categories = ",".join(hit.category for hit in hits)
        async with self._tx() as s:
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.MEMORY_WRITE_BLOCKED,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_WRITE.value,
                    tool_name=ToolName.MEMORY_WRITE.value,
                    outcome=AuditOutcome.DENIED,
                    request_summary=(
                        f"memory.write op={op_label} kind={kind.value if kind else '-'}"
                    ),
                    # Categories only — the secret VALUES never reach the audit log.
                    result_summary=f"secret blocked: categories={categories}",
                    error_code=ErrorCode.MEMORY_SECRET_DETECTED,
                ),
                sa_session=s,
            )
        raise JarvisException(
            _error(
                ErrorCode.MEMORY_SECRET_DETECTED,
                "memory.secret_detected",
                correlation_id,
                category=ErrorCategory.POLICY,
            )
        )

    async def _get_for_write(
        self, target_id: str, op_label: str, correlation_id: str
    ) -> MemoryItem:
        target = await self.memory.get(target_id)
        if target is None:
            raise JarvisException(_error(ErrorCode.NOT_FOUND, "memory.not_found", correlation_id))
        return target

    def _terminal_error(self, correlation_id: str) -> JarvisException:
        return JarvisException(
            _error(
                ErrorCode.INPUT_INVALID,
                "memory.invalid_transition",
                correlation_id,
                source=ErrorSource.USER,
            )
        )

    # -- writes -------------------------------------------------------------------

    async def create(
        self,
        *,
        kind: MemoryKind,
        title: str,
        content: str,
        confidence: float = 1.0,
        sensitivity: Sensitivity = Sensitivity.STANDARD,
        valid_until: AwareDatetime | None = None,
        session_id: str | None = None,
        source_message_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryItem:
        args = self._args(
            MemoryWriteOp.CREATE,
            correlation_id,
            kind=kind,
            title=title,
            content=content,
            confidence=confidence,
            sensitivity=sensitivity,
            valid_until=valid_until,
            origin="user_explicit_command",
        )
        if args.valid_until is not None and args.valid_until <= self.clock.now():
            raise JarvisException(
                _error(ErrorCode.INPUT_INVALID, "memory.invalid_args", correlation_id)
            )
        request = self._request(
            ToolName.MEMORY_WRITE, Capability.MEMORY_WRITE, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )
        await self._scan_or_block(
            f"{args.title}\n{args.content}",
            op_label="create",
            kind=args.kind,
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        async with self._tx() as s:
            now = self.clock.now()
            item = MemoryItem(
                kind=args.kind,  # type: ignore[arg-type]  # validated: create requires kind
                title=args.title or "",
                content=args.content or "",
                provenance=Provenance.USER_EXPLICIT,
                confidence=args.confidence if args.confidence is not None else 1.0,
                sensitivity=args.sensitivity,
                status=MemoryStatus.ACTIVE,
                session_id=session_id,
                source_message_id=source_message_id,
                valid_until=args.valid_until,
                created_at=now,
                updated_at=now,
            )
            await self.memory.create(item, sa_session=s)
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.MEMORY_CREATED,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_WRITE.value,
                    tool_name=ToolName.MEMORY_WRITE.value,
                    outcome=AuditOutcome.SUCCESS,
                    request_summary=_request_summary(request),
                    result_summary=f"id={item.id} status=active",
                ),
                sa_session=s,
            )
            return item

    async def supersede(
        self,
        target_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        confidence: float | None = None,
        sensitivity: Sensitivity | None = None,
        valid_until: AwareDatetime | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryItem:
        args = self._args(
            MemoryWriteOp.SUPERSEDE,
            correlation_id,
            target_id=target_id,
            title=title,
            content=content,
            confidence=confidence,
            sensitivity=sensitivity or Sensitivity.STANDARD,
            valid_until=valid_until,
            origin="user_explicit_command",
        )
        target = await self._get_for_write(target_id, "supersede", correlation_id)
        new_title = args.title or target.title
        new_content = args.content or target.content
        request = self._request(
            ToolName.MEMORY_WRITE, Capability.MEMORY_WRITE, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )
        await self._scan_or_block(
            f"{new_title}\n{new_content}",
            op_label="supersede",
            kind=target.kind,
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        async with self._tx() as s:
            now = self.clock.now()
            new_item = MemoryItem(
                kind=target.kind,
                title=new_title,
                content=new_content,
                provenance=target.provenance,
                confidence=args.confidence if args.confidence is not None else target.confidence,
                sensitivity=sensitivity or target.sensitivity,
                status=MemoryStatus.ACTIVE,
                session_id=session_id,
                valid_until=args.valid_until or target.valid_until,
                created_at=now,
                updated_at=now,
            )
            # Insert the successor BEFORE linking the target to it: superseded_by
            # is a self-referencing FK, so the new row must exist first. Same
            # transaction, so a failed link still rolls the insert back.
            await self.memory.create(new_item, sa_session=s)
            updated = await self.memory.set_status(
                target_id,
                MemoryStatus.SUPERSEDED,
                expected_from=_WRITEABLE_FROM,
                superseded_by=new_item.id,
                sa_session=s,
            )
            if updated is None:
                current = await self.memory.get(target_id)
                raise JarvisException(
                    _error(
                        ErrorCode.NOT_FOUND if current is None else ErrorCode.INPUT_INVALID,
                        "memory.not_found" if current is None else "memory.invalid_transition",
                        correlation_id,
                    )
                )
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.MEMORY_SUPERSEDED,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_WRITE.value,
                    tool_name=ToolName.MEMORY_WRITE.value,
                    outcome=AuditOutcome.SUCCESS,
                    request_summary=_request_summary(request),
                    result_summary=f"old={target_id} new={new_item.id}",
                ),
                sa_session=s,
            )
            return new_item

    async def _terminal_op(
        self,
        op: MemoryWriteOp,
        event: AuditEventType,
        to_status: MemoryStatus,
        target_id: str,
        *,
        session_id: str | None,
        task_id: str | None,
        correlation_id: str,
    ) -> MemoryItem:
        args = self._args(op, correlation_id, target_id=target_id, origin="user_explicit_command")
        target = await self._get_for_write(target_id, op.value, correlation_id)
        if target.status in TERMINAL_STATUSES:
            raise self._terminal_error(correlation_id)
        request = self._request(
            ToolName.MEMORY_WRITE, Capability.MEMORY_WRITE, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )
        async with self._tx() as s:
            updated = await self.memory.set_status(
                target_id, to_status, expected_from=_WRITEABLE_FROM, sa_session=s
            )
            if updated is None:
                # Lost a race, or the target was already terminal: re-check.
                current = await self.memory.get(target_id)
                if current is None:
                    raise JarvisException(
                        _error(ErrorCode.NOT_FOUND, "memory.not_found", correlation_id)
                    )
                raise self._terminal_error(correlation_id)
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=event,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_WRITE.value,
                    tool_name=ToolName.MEMORY_WRITE.value,
                    outcome=AuditOutcome.SUCCESS,
                    request_summary=_request_summary(request),
                    result_summary=f"id={target_id} status={to_status.value}",
                ),
                sa_session=s,
            )
            return updated

    async def revoke(
        self,
        target_id: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryItem:
        return await self._terminal_op(
            MemoryWriteOp.REVOKE,
            AuditEventType.MEMORY_REVOKED,
            MemoryStatus.REVOKED,
            target_id,
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )

    async def delete(
        self,
        target_id: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryItem:
        """Tombstone delete: the row stays, the status becomes deleted, the
        FTS trigger drops it from the index. Retrieval never returns it."""
        return await self._terminal_op(
            MemoryWriteOp.DELETE,
            AuditEventType.MEMORY_DELETED,
            MemoryStatus.DELETED,
            target_id,
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )

    async def confirm(
        self,
        target_id: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryItem:
        """Confirm a pending item (explicit user confirmation). Only pending
        items can be confirmed; anything else is a contract violation."""
        args = self._args(
            MemoryWriteOp.CONFIRM,
            correlation_id,
            target_id=target_id,
            origin="user_explicit_command",
        )
        target = await self._get_for_write(target_id, "confirm", correlation_id)
        if target.status is not MemoryStatus.PENDING:
            raise self._terminal_error(correlation_id)
        request = self._request(
            ToolName.MEMORY_WRITE, Capability.MEMORY_WRITE, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )
        async with self._tx() as s:
            updated = await self.memory.set_status(
                target_id,
                MemoryStatus.ACTIVE,
                expected_from=frozenset({MemoryStatus.PENDING}),
                sa_session=s,
            )
            if updated is None:
                raise self._terminal_error(correlation_id)
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.MEMORY_CONFIRMED,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_WRITE.value,
                    tool_name=ToolName.MEMORY_WRITE.value,
                    outcome=AuditOutcome.SUCCESS,
                    request_summary=_request_summary(request),
                    result_summary=f"id={target_id} status=active",
                ),
                sa_session=s,
            )
            return updated

    async def purge(
        self,
        target_id: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> None:
        """Physical DELETE. Only a soft-deleted item can be purged: the
        two-step (delete tombstone, then purge) keeps physical deletion
        deliberate. Provenance links (superseded_by) pointing at the
        target are cleared first, in the same transaction."""
        args = self._args(
            MemoryWriteOp.PURGE, correlation_id, target_id=target_id, origin="user_explicit_command"
        )
        target = await self._get_for_write(target_id, "purge", correlation_id)
        if target.status is not MemoryStatus.DELETED:
            raise JarvisException(
                _error(ErrorCode.INPUT_INVALID, "memory.invalid_transition", correlation_id)
            )
        request = self._request(
            ToolName.MEMORY_WRITE, Capability.MEMORY_WRITE, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )
        async with self._tx() as s:
            await self.memory.clear_superseded_by(target_id, sa_session=s)
            deleted = await self.memory.purge(target_id, sa_session=s)
            if not deleted:
                raise JarvisException(
                    _error(ErrorCode.NOT_FOUND, "memory.not_found", correlation_id)
                )
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.MEMORY_PURGED,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_WRITE.value,
                    tool_name=ToolName.MEMORY_WRITE.value,
                    outcome=AuditOutcome.SUCCESS,
                    request_summary=_request_summary(request),
                    result_summary=f"id={target_id} kind={target.kind.value}",
                ),
                sa_session=s,
            )

    # -- reads --------------------------------------------------------------------

    async def read(
        self,
        query: MemoryQuery,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryReadResult:
        """Authorized read: policy first, then the read-only retrieval."""
        args = MemoryReadArgs(query=query)
        request = self._request(
            ToolName.MEMORY_READ, Capability.MEMORY_READ, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )
        hits = await self.memory.retrieve(query)
        return MemoryReadResult(hits=tuple(hits), query=query.query)

    async def retrieve(self, query: MemoryQuery) -> list[MemoryHit]:
        """Raw read-only retrieval. The caller must have authorized already."""
        return await self.memory.retrieve(query)

    async def _authorize_read(
        self,
        *,
        query_label: str,
        session_id: str | None,
        task_id: str | None,
        correlation_id: str,
    ) -> None:
        """Policy gate for capability-level memory reads (list/get/export).

        The decision is about the capability, not about any query text, so a
        fixed label query is used. The decision audit commits in its own
        transaction so a DENY is durable.
        """
        args = MemoryReadArgs(query=MemoryQuery(query=query_label, limit=1))
        request = self._request(
            ToolName.MEMORY_READ, Capability.MEMORY_READ, args, task_id, correlation_id
        )
        await self._authorize(
            request, session_id=session_id, task_id=task_id, correlation_id=correlation_id
        )

    async def get_authorized(
        self,
        item_id: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> MemoryItem | None:
        await self._authorize_read(
            query_label="inspection",
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        return await self.memory.get(item_id)

    async def list_authorized(
        self,
        *,
        kinds: frozenset[MemoryKind] | None = None,
        statuses: frozenset[MemoryStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> list[MemoryItem]:
        """Authorized listing: policy first (memory.read), then the read-only list.

        The policy check runs on a capability-only read request — the decision
        is about the capability, not about any query text.
        """
        await self._authorize_read(
            query_label="list",
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        return await self.memory.list_items(
            kinds=kinds, statuses=statuses, limit=limit, offset=offset
        )

    async def list_items(
        self,
        *,
        kinds: frozenset[MemoryKind] | None = None,
        statuses: frozenset[MemoryStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryItem]:
        return await self.memory.list_items(
            kinds=kinds, statuses=statuses, limit=limit, offset=offset
        )

    async def export(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> list[MemoryItem]:
        """Full JSON export. Policy-checked as a memory.read; audited once."""
        await self._authorize_read(
            query_label="export",
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        async with self._tx() as s:
            items = await self.memory.list_items(limit=_EXPORT_LIMIT)
            await self.audit.append(
                AuditLog(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=AuditEventType.MEMORY_EXPORTED,
                    actor=AuditActor.LOCAL_USER,
                    capability=Capability.MEMORY_READ.value,
                    tool_name=ToolName.MEMORY_READ.value,
                    outcome=AuditOutcome.SUCCESS,
                    request_summary="memory.read export",
                    result_summary=f"items={len(items)}",
                ),
                sa_session=s,
            )
            return items


__all__ = ["MemoryService"]
