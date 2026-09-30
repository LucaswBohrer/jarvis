"""Orchestrator: the fixed NEXUS_STATUS pipeline.

Pipeline (each audited, each guarded):
  normalize input -> validate session -> persist message -> recognize intent
  -> create task (PENDING) -> persist plan (PLANNING) -> evaluate policy
  -> execute nexus.status (RUNNING) -> verify result -> build grounded facts
  -> call LLM (timeout + grounding check, else deterministic fallback)
  -> render -> persist assistant message -> COMPLETED.

NEXUS unavailable (including a tool-call timeout) is a successful answer
(COMPLETED/UNAVAILABLE), not a failure. The LLM never executes tools and its
output is always re-verified. Cancellation propagates as CancelledError and
is recorded as CANCELLED.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import AwareDatetime, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.persistence.database import Database
from ..adapters.persistence.repositories import (
    SqlAuditRepository,
    SqlMessageRepository,
    SqlSessionRepository,
    SqlTaskRepository,
)
from ..config import Settings
from ..domain.contracts.audit import (
    AuditActor,
    AuditEventType,
    AuditLog,
    AuditOutcome,
)
from ..domain.contracts.commitments import CommitmentStatus
from ..domain.contracts.common import new_id
from ..domain.contracts.common import utcnow as _utcnow
from ..domain.contracts.context import CommitmentCtx, ContextBudgets, ContextSnapshot
from ..domain.contracts.llm import LLMResponse, NexusResponsePlan
from ..domain.contracts.memory import (
    MemoryHit,
    MemoryKind,
    MemoryQuery,
    MemoryStatus,
    Provenance,
    Sensitivity,
)
from ..domain.contracts.nexus import NexusStatusQuery, VerifiedNexusStatus
from ..domain.contracts.policy import PolicyEffect
from ..domain.contracts.session import MessageRole, UserMessage
from ..domain.contracts.task import (
    AssistantResponse,
    Intent,
    ResponseSource,
    ResponseStatus,
    TaskInput,
    TaskKind,
    TaskOutcome,
    TaskPlan,
    TaskRecord,
    TaskResult,
    TaskState,
)
from ..domain.contracts.tool import (
    Capability,
    ToolName,
    ToolOutcome,
    ToolRequest,
    ToolResult,
)
from ..domain.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorSource,
    JarvisError,
    JarvisException,
)
from ..domain.state_machine import transition
from ..ports.clock import Clock, SystemClock
from ..ports.llm import LLMError, LLMProvider
from ..ports.nexus import NexusIntegration
from ..ports.repositories import TaskConflictError
from ..security.policy import PolicyEngine
from ..security.redaction import redact
from ..verification.nexus import verify_nexus_result
from ..verification.response import GroundingError, verify_plan
from .commitments import (
    USER_EXPLICIT_ORIGIN,
    CommitmentService,
    split_due_expression,
)
from .context_builder import ContextBuilder, ContextBuildInput
from .context_service import build_llm_request
from .memory_service import MemoryService
from .response_service import (
    build_fallback_plan,
    render_assistant_response,
    render_commitment_create_response,
    render_commitment_fulfill_response,
    render_memory_read_response,
    render_memory_write_response,
)

log = logging.getLogger(__name__)

# Normalized fragments that mark an explicit commitment (charge) command.
# Priority: MEMORY_WRITE > MEMORY_READ > COMMITMENT_CREATE > COMMITMENT_FULFILL
# > NEXUS_STATUS. Commitments come after memory because "lembre-se de me
# cobrar..." is a memory write, not a charge.
_COMMITMENT_CREATE_FRAGMENTS = (
    "me cobre de",
    "me cobra de",
    "me lembre de",
    "cobre me de",
    "cobrar de",
)

# Normalized fragments that mark an explicit fulfillment of a commitment.
_COMMITMENT_FULFILL_FRAGMENTS = (
    "conclui",
    "finalizei",
    "terminei",
    "ja fiz",
    "marcar como feito",
    "dar baixa",
)

# Raw (case-insensitive) prefixes stripped by the commitment parsers.
_COMMITMENT_CREATE_PREFIXES = (
    "me cobre de",
    "me cobra de",
    "me lembre de",
    "cobre-me de",
    "cobre me de",
    "me cobre",
    "me cobra",
)

# Raw (case-insensitive) prefixes stripped by the fulfillment parser.
_COMMITMENT_FULFILL_PREFIXES = (
    "concluí",
    "conclui",
    "finalizei",
    "terminei",
    "já fiz",
    "ja fiz",
    "marcar como feito",
    "marque como feito",
    "dê baixa em",
    "de baixa em",
    "dar baixa em",
)
# Normalized (accent-stripped, lowercased) fragments that mark a NEXUS status ask.
_INTENT_FRAGMENTS = (
    "como esta",
    "como vai",
    "como anda",
    "status",
    "tudo bem",
    "situacao",
    "funcionando",
)

# Normalized fragments that mark an explicit memory-write command.
# Priority: MEMORY_WRITE > MEMORY_READ > COMMITMENT_CREATE > COMMITMENT_FULFILL > NEXUS_STATUS.
_MEMORY_WRITE_FRAGMENTS = (
    "lembre-se",
    "lembre se",
    "memorize",
    "anote",
    "guarde",
    "nao se esqueca",
    "nao esqueca",
)

# Normalized fragments that mark an explicit memory-read command.
_MEMORY_READ_FRAGMENTS = (
    "o que voce sabe sobre",
    "o que sabe sobre",
    "voce sabe sobre",
    "voce se lembra",
    "voce lembra",
    "minhas memorias",
    "suas memorias",
    "que memorias",
)

# Raw (case-insensitive) prefixes stripped by the memory parsers. Longest
# first so "o que você sabe sobre" wins over "você sabe sobre".
_MEMORY_WRITE_PREFIXES = (
    "não se esqueça de",
    "nao se esqueca de",
    "não se esqueça",
    "nao se esqueca",
    "não esqueça de",
    "nao esqueca de",
    "não esqueça",
    "nao esqueca",
    "lembre-se de",
    "lembre se de",
    "lembre-se",
    "lembre se",
    "memorize",
    "anote",
    "guarde",
)

_MEMORY_READ_PREFIXES = (
    "o que você sabe sobre",
    "o que voce sabe sobre",
    "o que sabe sobre",
    "você sabe sobre",
    "voce sabe sobre",
    "você se lembra de",
    "voce se lembra de",
    "você lembra de",
    "voce lembra de",
    "minhas memórias sobre",
    "minhas memorias sobre",
    "minhas memórias",
    "minhas memorias",
    "suas memórias",
    "suas memorias",
)

# Leading connectors/articles stripped after the command prefix. Each word
# alternative requires a following separator (or end of string) so that words
# merely *starting* with an article letter ("apagar", "dedo", "amor") are not
# eaten — "a" alone strips, "apagar" does not.
_LEAD_STRIP_RE = re.compile(
    r"^(?:(?:de que|de|que|sobre|do|da|dos|das|o|a|os|as)(?=[\s:,\-—]|$)|[\s:,\-—])+",
    re.IGNORECASE,
)

# Hints that the written fact is a preference rather than a plain fact.
_PREFERENCE_HINTS = (
    "prefiro",
    "prefere",
    "gosto de",
    "gosta de",
    "nao gosto",
    "não gosto",
    "odeio",
    "quero que",
)

_PLAN_STEPS = (
    "validate_input",
    "evaluate_policy",
    "call_nexus",
    "verify_result",
    "generate_plan",
    "render_response",
    "persist",
)

_MEMORY_WRITE_PLAN_STEPS = (
    "recognize_intent",
    "evaluate_policy",
    "scan_secrets",
    "persist_memory",
    "render_response",
    "persist",
)

_MEMORY_READ_PLAN_STEPS = (
    "recognize_intent",
    "evaluate_policy",
    "retrieve_memory",
    "render_response",
    "persist",
)

_COMMITMENT_CREATE_PLAN_STEPS = (
    "recognize_intent",
    "create_commitment",
    "render_response",
    "persist",
)

_COMMITMENT_FULFILL_PLAN_STEPS = (
    "recognize_intent",
    "match_commitment",
    "fulfill_commitment",
    "render_response",
    "persist",
)


def _normalize(text: str) -> str:
    lowered = text.lower()
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9 ]", " ", stripped)


def recognize_intent(text: str) -> Intent | None:
    """Pure intent recognition (deterministic, no LLM).

    Priority: MEMORY_WRITE > MEMORY_READ > COMMITMENT_CREATE >
    COMMITMENT_FULFILL > NEXUS_STATUS. Anything else — including
    instruction-override attempts — returns None and the task fails without
    executing.
    """
    normalized = _normalize(text)
    if any(frag in normalized for frag in _MEMORY_WRITE_FRAGMENTS):
        return Intent.MEMORY_WRITE
    if any(frag in normalized for frag in _MEMORY_READ_FRAGMENTS):
        return Intent.MEMORY_READ
    if any(frag in normalized for frag in _COMMITMENT_CREATE_FRAGMENTS):
        return Intent.COMMITMENT_CREATE
    if any(frag in normalized for frag in _COMMITMENT_FULFILL_FRAGMENTS):
        return Intent.COMMITMENT_FULFILL
    if "nexus" in normalized and any(frag in normalized for frag in _INTENT_FRAGMENTS):
        return Intent.NEXUS_STATUS
    return None


def parse_memory_write(text: str) -> tuple[MemoryKind, str, str] | None:
    """Parse an explicit memory-write command.

    Returns (kind, title, content), or None when the text after the command
    prefix is empty or exceeds the content limit. Deterministic: no LLM.
    """
    lowered = text.lower()
    rest: str | None = None
    for prefix in _MEMORY_WRITE_PREFIXES:
        idx = lowered.find(prefix)
        if idx != -1:
            rest = text[idx + len(prefix) :]
            break
    if rest is None:
        return None
    rest = _LEAD_STRIP_RE.sub("", rest)
    rest = " ".join(rest.split())
    if not rest or len(rest) > 4000:
        return None
    normalized = _normalize(rest)
    kind = (
        MemoryKind.PREFERENCE
        if any(hint in normalized for hint in _PREFERENCE_HINTS)
        else MemoryKind.FACT
    )
    title = rest if len(rest) <= 120 else _truncate_title(rest)
    return kind, title, rest


def _truncate_title(text: str) -> str:
    """Truncate to 120 chars, preferring a word boundary."""
    head = text[:117].rsplit(" ", 1)[0]
    return (head if head else text[:117]) + "..."


def parse_memory_read(text: str) -> str | None:
    """Parse an explicit memory-read command.

    Returns the query text ("" = list everything), or None when no read
    prefix is found. Deterministic: no LLM.
    """
    lowered = text.lower()
    rest: str | None = None
    for prefix in _MEMORY_READ_PREFIXES:
        idx = lowered.find(prefix)
        if idx != -1:
            rest = text[idx + len(prefix) :]
            break
    if rest is None:
        return None
    rest = _LEAD_STRIP_RE.sub("", rest)
    return " ".join(rest.split())


def parse_commitment_create(
    text: str, now: AwareDatetime
) -> tuple[str, AwareDatetime | None] | None:
    """Parse an explicit commitment command ("me cobre de X até amanhã").

    Returns (title, due_at), or None when nothing usable follows the prefix.
    The trailing due expression is split by split_due_expression; an
    unparsable date stays in the title with due_at=None (D41). Empty titles
    return None. Deterministic: no LLM.
    """
    lowered = text.lower()
    rest: str | None = None
    for prefix in _COMMITMENT_CREATE_PREFIXES:
        idx = lowered.find(prefix)
        if idx != -1:
            rest = text[idx + len(prefix) :]
            break
    if rest is None:
        return None
    rest = _LEAD_STRIP_RE.sub("", rest)
    rest = " ".join(rest.split())
    if not rest:
        return None
    title, due_at = split_due_expression(rest, now)
    title = title if len(title) <= 200 else _truncate_title(title)[:200]
    if not title:
        return None
    return title, due_at


def parse_commitment_fulfill(text: str) -> str | None:
    """Parse an explicit fulfillment command ("concluí X").

    Returns the match text used to find the open commitment, or None when
    nothing usable follows the prefix. Deterministic: no LLM.
    """
    lowered = text.lower()
    rest: str | None = None
    for prefix in _COMMITMENT_FULFILL_PREFIXES:
        idx = lowered.find(prefix)
        if idx != -1:
            rest = text[idx + len(prefix) :]
            break
    if rest is None:
        return None
    rest = _LEAD_STRIP_RE.sub("", rest)
    rest = " ".join(rest.split())
    return rest or None


def _error(
    code: ErrorCode,
    category: ErrorCategory,
    user_message_key: str,
    correlation_id: str,
    *,
    source: ErrorSource = ErrorSource.SYSTEM,
    retryable: bool = False,
) -> JarvisError:
    return JarvisError(
        code=code,
        category=category,
        user_message_key=user_message_key,
        retryable=retryable,
        source=source,
        correlation_id=correlation_id,
    )


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _ContextBudgetExceeded(Exception):
    """Control flow: nexus_facts exceeded their budget, so the LLM stage is
    skipped (fail-closed) and the deterministic renderer answers."""


def _request_summary(request: ToolRequest) -> str:
    args = redact(request.arguments.model_dump(mode="json"))
    return (
        f"{request.tool_name.value} capability={request.capability.value} "
        f"arguments={json.dumps(args, sort_keys=True, default=str)[:400]}"
    )


@dataclass
class Orchestrator:
    settings: Settings
    db: Database
    sessions: SqlSessionRepository
    messages: SqlMessageRepository
    tasks: SqlTaskRepository
    audit: SqlAuditRepository
    policy: PolicyEngine
    nexus: NexusIntegration
    llm: LLMProvider
    memory_service: MemoryService
    commitment_service: CommitmentService
    clock: Clock = field(default_factory=SystemClock)
    context_builder: ContextBuilder = field(default_factory=ContextBuilder)
    _cancel_events: dict[str, asyncio.Event] = field(default_factory=dict, init=False, repr=False)

    # -- cancellation ----------------------------------------------------

    def cancel_task(self, task_id: str) -> bool:
        """Signal cooperative cancellation. True if a running task knew it."""
        event = self._cancel_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    # -- transactions -----------------------------------------------------

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self.db.session() as s, s.begin():
            yield s

    async def _audit(
        self,
        s: AsyncSession,
        *,
        session_id: str | None,
        task_id: str | None,
        event: AuditEventType,
        correlation_id: str,
        outcome: AuditOutcome,
        capability: str | None = None,
        tool_name: str | None = None,
        decision: PolicyEffect | None = None,
        request_summary: str | None = None,
        result_summary: str | None = None,
        result_digest: str | None = None,
        error_code: ErrorCode | None = None,
        duration_ms: int | None = None,
    ) -> None:
        entry = AuditLog(
            correlation_id=correlation_id,
            session_id=session_id,
            task_id=task_id,
            event_type=event,
            actor=AuditActor.SYSTEM,
            capability=capability,
            tool_name=tool_name,
            decision=decision,
            outcome=outcome,
            request_summary=request_summary,
            result_summary=result_summary,
            result_digest=result_digest,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        await self.audit.append(entry, sa_session=s)

    async def _transition(
        self,
        s: AsyncSession,
        task: TaskRecord,
        to_state: TaskState,
        *,
        current_step: str | None = None,
    ) -> TaskRecord:
        expected = task.version
        moved = transition(task, to_state, current_step=current_step)
        return await self.tasks.update(moved, expected_version=expected, sa_session=s)

    async def _touch_step(self, s: AsyncSession, task: TaskRecord, step: str) -> TaskRecord:
        """Advance current_step inside RUNNING (no state transition)."""
        expected = task.version
        touched = task.model_copy(
            update={
                "current_step": step,
                "version": task.version + 1,
                "updated_at": _utcnow(),
            }
        )
        return await self.tasks.update(touched, expected_version=expected, sa_session=s)

    async def _fail_task(
        self,
        s: AsyncSession,
        task: TaskRecord,
        error: JarvisError,
        *,
        session_id: str,
        current_step: str | None = None,
    ) -> TaskRecord:
        failed = task.model_copy(update={"error_code": error.code})
        moved = await self._transition(s, failed, TaskState.FAILED, current_step=current_step)
        await self._audit(
            s,
            session_id=session_id,
            task_id=task.id,
            event=AuditEventType.TASK_FAILED,
            correlation_id=error.correlation_id,
            outcome=AuditOutcome.FAILURE,
            error_code=error.code,
        )
        return moved

    # -- cancellable awaits ------------------------------------------------

    async def _await_cancellable(
        self,
        coro: Any,
        task_id: str,
        timeout_s: float,
    ) -> Any:
        """Await coro, racing a cooperative cancel event and a timeout.

        CancelledError -> user/system cancellation (propagates untouched).
        TimeoutError -> the operation exceeded its deadline.
        """
        event = self._cancel_events.get(task_id)
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(event.wait()) if event is not None else None
        watch = {task} | ({waiter} if waiter is not None else set())
        try:
            done, _ = await asyncio.wait(
                watch, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            # Cancel won or the deadline expired: stop the operation.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            if event is not None and event.is_set():
                raise asyncio.CancelledError("task cancelled")
            raise TimeoutError(f"operation exceeded {timeout_s:.1f}s")
        finally:
            if waiter is not None:
                waiter.cancel()

    def _raise_if_cancelled(self, task_id: str) -> None:
        """Cooperative cancellation checkpoint between pipeline phases."""
        event = self._cancel_events.get(task_id)
        if event is not None and event.is_set():
            raise asyncio.CancelledError("task cancelled")

    # -- main pipeline -----------------------------------------------------

    async def handle_message(
        self,
        session_id: str,
        content: str,
        *,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> AssistantResponse:
        correlation_id = correlation_id or new_id()

        session = await self.sessions.get(session_id)
        if session is None:
            raise JarvisException(
                _error(
                    ErrorCode.NOT_FOUND,
                    ErrorCategory.VALIDATION,
                    "session.not_found",
                    correlation_id,
                )
            )

        # Lazy sweep: expire overdue commitments (and expired memories)
        # before handling the message. Best-effort and self-throttled by
        # the 1h service_meta cooldown; never blocks the conversation.
        try:
            await self.commitment_service.sweep(
                session_id=session_id, correlation_id=correlation_id
            )
        except Exception as exc:  # noqa: BLE001 - sweep must never fail the chat
            log.warning("commitment sweep failed (best-effort): %s", exc)

        # Idempotency: a repeated key replays the original assistant response.
        if idempotency_key:
            replay = await self._replay_if_exists(session_id, idempotency_key)
            if replay is not None:
                return replay

        try:
            user_msg = UserMessage(
                session_id=session_id,
                content=content,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            raise JarvisException(
                _error(
                    ErrorCode.INPUT_INVALID,
                    ErrorCategory.VALIDATION,
                    "message.invalid",
                    correlation_id,
                    source=ErrorSource.USER,
                )
            ) from None

        try:
            async with self._tx() as s:
                await self.messages.add(user_msg, sa_session=s)
        except IntegrityError:
            # Lost a race with a duplicate key: replay the winner.
            if idempotency_key:
                replay = await self._replay_if_exists(session_id, idempotency_key)
                if replay is not None:
                    return replay
            raise JarvisException(
                _error(
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "internal.error",
                    correlation_id,
                )
            ) from None

        # recognize intent (pure; no side effects)
        intent = recognize_intent(user_msg.content)

        if intent is Intent.MEMORY_WRITE:
            task_kind = TaskKind.MEMORY_WRITE
        elif intent is Intent.MEMORY_READ:
            task_kind = TaskKind.MEMORY_READ
        elif intent is Intent.COMMITMENT_CREATE:
            task_kind = TaskKind.COMMITMENT_CREATE
        elif intent is Intent.COMMITMENT_FULFILL:
            task_kind = TaskKind.COMMITMENT_FULFILL
        else:
            task_kind = TaskKind.NEXUS_STATUS
        task = TaskRecord(
            session_id=session_id,
            kind=task_kind,
            state=TaskState.PENDING,
            input=TaskInput(
                message_id=user_msg.id,
                utterance=user_msg.content,
                intent=intent,
            ),
        )
        self._cancel_events[task.id] = asyncio.Event()

        try:
            # create task (one unit of work with its audit event)
            async with self._tx() as s:
                await self.tasks.create(task, sa_session=s)
                await self.messages.set_task_id(user_msg.id, task.id, sa_session=s)
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.TASK_CREATED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.SUCCESS,
                )

            if intent is None:
                # Injection/unsupported input: audited, never executed.
                error = _error(
                    ErrorCode.INTENT_UNSUPPORTED,
                    ErrorCategory.VALIDATION,
                    "intent.unsupported",
                    correlation_id,
                    source=ErrorSource.USER,
                )
                async with self._tx() as s:
                    await self._fail_task(
                        s,
                        task,
                        error,
                        session_id=session_id,
                        current_step="validate_input",
                    )
                raise JarvisException(error)

            if intent is Intent.MEMORY_WRITE:
                return await self._handle_memory_write(
                    task, user_msg, session_id=session_id, correlation_id=correlation_id
                )
            if intent is Intent.MEMORY_READ:
                return await self._handle_memory_read(
                    task, user_msg, session_id=session_id, correlation_id=correlation_id
                )
            if intent is Intent.COMMITMENT_CREATE:
                return await self._handle_commitment_create(
                    task, user_msg, session_id=session_id, correlation_id=correlation_id
                )
            if intent is Intent.COMMITMENT_FULFILL:
                return await self._handle_commitment_fulfill(
                    task, user_msg, session_id=session_id, correlation_id=correlation_id
                )

            # persist fixed plan -> PLANNING
            self._raise_if_cancelled(task.id)
            plan = TaskPlan(
                task_id=task.id,
                steps=list(_PLAN_STEPS),
                capability_set=[Capability.NEXUS_STATUS_READ.value],
                deadline_at=self.clock.now()
                + timedelta(milliseconds=self.settings.nexus_total_deadline_ms),
            )
            async with self._tx() as s:
                task = await self._transition(
                    s,
                    task.model_copy(update={"plan": plan}),
                    TaskState.PLANNING,
                    current_step="validate_input",
                )
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.TASK_PLANNED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.SUCCESS,
                )

            # policy evaluation -> RUNNING
            tool_request = ToolRequest(
                task_id=task.id,
                correlation_id=correlation_id,
                tool_name=ToolName.NEXUS_STATUS,
                capability=Capability.NEXUS_STATUS_READ,
                arguments=NexusStatusQuery(equipment_code=self.settings.nexus_equipment_code),
                requested_at=self.clock.now(),
                deadline_at=plan.deadline_at,
            )
            policy_url = f"{self.settings.nexus_base_url}/api/v1/equipment"
            decision = self.policy.decide(tool_request, method="GET", url=policy_url)
            async with self._tx() as s:
                task = await self._transition(
                    s, task, TaskState.RUNNING, current_step="evaluate_policy"
                )
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.POLICY_DECIDED,
                    correlation_id=correlation_id,
                    outcome=(
                        AuditOutcome.SUCCESS
                        if decision.decision is PolicyEffect.ALLOW
                        else AuditOutcome.DENIED
                    ),
                    capability=tool_request.capability.value,
                    tool_name=tool_request.tool_name.value,
                    decision=decision.decision,
                    request_summary=_request_summary(tool_request),
                )

            if decision.decision is PolicyEffect.DENY:
                error = _error(
                    ErrorCode.POLICY_DENIED,
                    ErrorCategory.POLICY,
                    "policy.denied",
                    correlation_id,
                    source=ErrorSource.POLICY,
                )
                async with self._tx() as s:
                    await self._fail_task(
                        s,
                        task,
                        error,
                        session_id=session_id,
                        current_step="evaluate_policy",
                    )
                raise JarvisException(error)
            if decision.decision is PolicyEffect.CONFIRM:
                # Not configured in Phase 1; the branch exists so a CONFIRM
                # rule can never silently become an ALLOW.
                error = _error(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    ErrorCategory.POLICY,
                    "policy.confirmation_required",
                    correlation_id,
                    source=ErrorSource.POLICY,
                )
                async with self._tx() as s:
                    moved = transition(
                        task,
                        TaskState.WAITING_CONFIRMATION,
                        current_step="evaluate_policy",
                    )
                    await self.tasks.update(moved, expected_version=task.version, sa_session=s)
                raise JarvisException(error)

            # execute tool (cancellable; every outcome audited)
            self._raise_if_cancelled(task.id)
            tool_started_at = self.clock.now()
            async with self._tx() as s:
                task = await self._touch_step(s, task, "call_nexus")
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.TOOL_STARTED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.SUCCESS,
                    capability=tool_request.capability.value,
                    tool_name=tool_request.tool_name.value,
                    request_summary=_request_summary(tool_request),
                )
            remaining = max(
                0.2,
                (tool_request.deadline_at - self.clock.now()).total_seconds(),
            )
            try:
                tool_result = await self._await_cancellable(
                    self.nexus.get_status(tool_request), task.id, remaining
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # Backstop: the adapter enforces the deadline itself, but the
                # orchestrator never lets a tool call outlive the plan. A
                # timed-out tool is NEXUS unavailable, not a task failure.
                tool_result = ToolResult(
                    request_id=tool_request.request_id,
                    task_id=task.id,
                    outcome=ToolOutcome.FAILURE,
                    data=None,
                    error=_error(
                        ErrorCode.NEXUS_TIMEOUT,
                        ErrorCategory.INTEGRATION,
                        "nexus.timeout",
                        correlation_id,
                        source=ErrorSource.NEXUS,
                        retryable=True,
                    ),
                    started_at=tool_started_at,
                    finished_at=self.clock.now(),
                )
            tool_duration_ms = int((self.clock.now() - tool_started_at).total_seconds() * 1000)
            async with self._tx() as s:
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=(
                        AuditEventType.TOOL_COMPLETED
                        if tool_result.outcome is ToolOutcome.SUCCESS
                        else AuditEventType.TOOL_FAILED
                    ),
                    correlation_id=correlation_id,
                    outcome=(
                        AuditOutcome.SUCCESS
                        if tool_result.outcome is ToolOutcome.SUCCESS
                        else AuditOutcome.FAILURE
                    ),
                    capability=tool_request.capability.value,
                    tool_name=tool_request.tool_name.value,
                    result_summary=(
                        f"outcome={tool_result.outcome.value} "
                        f"calls={len(tool_result.evidence)}"
                        + (f" error={tool_result.error.code.value}" if tool_result.error else "")
                    ),
                    result_digest=_digest(tool_result.model_dump(mode="json")),
                    error_code=(tool_result.error.code if tool_result.error else None),
                    duration_ms=tool_duration_ms,
                )

            # verify result (pure)
            self._raise_if_cancelled(task.id)
            verified = verify_nexus_result(
                tool_result,
                clock=self.clock,
                freshness_seconds=self.settings.nexus_freshness_seconds,
            )
            async with self._tx() as s:
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.VERIFICATION_COMPLETED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.SUCCESS,
                    result_summary=(
                        f"availability={verified.availability.value} "
                        f"data_state={verified.data_state.value} "
                        f"facts={len(verified.facts)}"
                    ),
                    result_digest=_digest([f.model_dump(mode="json") for f in verified.facts]),
                )

            # NEXUS unavailable: honest answer, no LLM.
            if verified.availability.value == "unavailable":
                return await self._finish_without_llm(task, verified, session_id, correlation_id)

            # Context assembly (pure): verified facts + retrieved memories +
            # conversation tail, under char budgets. The builder never runs
            # tools; retrieval below is the orchestrator's own read-only path.
            self._raise_if_cancelled(task.id)
            snapshot = await self._assemble_context(task.id, session_id, content, verified)
            async with self._tx() as s:
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.CONTEXT_ASSEMBLED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.SUCCESS,
                    result_summary=(
                        f"snapshot={snapshot.snapshot_id} "
                        f"sections={len(self.context_builder.sections_for_llm(snapshot))} "
                        f"truncated={','.join(snapshot.truncated_sections) or 'none'} "
                        f"llm_permitted={snapshot.llm_permitted}"
                    ),
                    result_digest=snapshot.digest,
                )

            # LLM plan with timeout + grounding verification, else fallback.
            # Fail-closed: when the verified facts exceeded their budget the
            # LLM stage is skipped and the deterministic renderer answers.
            self._raise_if_cancelled(task.id)
            sections = self.context_builder.sections_for_llm(snapshot)
            llm_request = build_llm_request(
                verified,
                task_id=task.id,
                locale=self.settings.default_locale,
                max_output_tokens=self.settings.llm_max_output_tokens,
                context_sections=sections,
            )
            plan_source = ResponseSource.LLM_PLAN
            llm_plan: NexusResponsePlan | None = None
            llm_started_at = self.clock.now()
            try:
                if not snapshot.llm_permitted:
                    raise _ContextBudgetExceeded()
                llm_response: LLMResponse = await self._await_cancellable(
                    self.llm.generate_structured(
                        llm_request,
                        timeout_seconds=self.settings.llm_timeout_seconds,
                    ),
                    task.id,
                    self.settings.llm_timeout_seconds,
                )
                llm_plan = verify_plan(llm_response.structured_output, verified)
                llm_event = AuditEventType.LLM_COMPLETED
                llm_outcome = AuditOutcome.SUCCESS
                llm_summary = f"model={llm_response.model} summary_key={llm_plan.summary_key.value}"
                llm_error: ErrorCode | None = None
            except asyncio.CancelledError:
                raise
            except _ContextBudgetExceeded:
                llm_event = AuditEventType.LLM_FALLBACK
                llm_outcome = AuditOutcome.FAILURE
                llm_summary = (
                    "context budget exceeded (nexus_facts over cap); deterministic fallback"
                )
                llm_error = ErrorCode.CONTEXT_BUDGET_EXCEEDED
            except (TimeoutError, LLMError) as exc:
                llm_event = AuditEventType.LLM_FALLBACK
                llm_outcome = AuditOutcome.FAILURE
                llm_summary = f"llm failed ({type(exc).__name__}); deterministic fallback"
                llm_error = (
                    ErrorCode.LLM_TIMEOUT
                    if isinstance(exc, TimeoutError)
                    else ErrorCode.LLM_OUTPUT_INVALID
                )
            except GroundingError as exc:
                llm_event = AuditEventType.LLM_FALLBACK
                llm_outcome = AuditOutcome.FAILURE
                llm_summary = f"llm plan not grounded: {exc}; deterministic fallback"
                llm_error = ErrorCode.RESPONSE_GROUNDING_FAILED
            llm_duration_ms = int((self.clock.now() - llm_started_at).total_seconds() * 1000)
            async with self._tx() as s:
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=llm_event,
                    correlation_id=correlation_id,
                    outcome=llm_outcome,
                    result_summary=llm_summary,
                    error_code=llm_error,
                    duration_ms=llm_duration_ms,
                )
            if llm_plan is None:
                plan_source = ResponseSource.DETERMINISTIC_FALLBACK
                llm_plan = build_fallback_plan(verified, task_id=task.id)

            # Preferences may only influence the presentation level (D37):
            # the deterministic override stands for both the LLM plan and
            # the fallback path.
            preferred = ContextBuilder.preferred_detail_level(snapshot.preferences)
            if preferred is not None:
                llm_plan = llm_plan.model_copy(update={"detail_level": preferred})

            # render + persist + COMPLETED (one unit of work)
            self._raise_if_cancelled(task.id)
            # "Você me disse:" notes: only non-sensitive memories the user
            # actually stated (never auto-persisted system events).
            memory_notes = [
                m
                for m in snapshot.memories
                if m.sensitivity is not Sensitivity.SENSITIVE
                and m.provenance is Provenance.USER_EXPLICIT
            ]
            response = render_assistant_response(
                verified,
                llm_plan,
                task_id=task.id,
                source=plan_source,
                memory_notes=memory_notes,
                # "📌 Lembretes": open commitments due within 24h, not
                # surfaced in the last 12h. Presentation only — no tool
                # call, no external action (T18).
                commitment_notes=snapshot.commitments_due,
            )
            assistant_msg = UserMessage(
                session_id=session_id,
                task_id=task.id,
                role=MessageRole.ASSISTANT,
                content=response.message,
            )
            task_result = TaskResult(
                outcome=(
                    TaskOutcome.UNAVAILABLE
                    if response.status is ResponseStatus.UNAVAILABLE
                    else TaskOutcome.SUCCESS
                ),
                response_status=response.status,
                assistant_message_id=assistant_msg.id,
            )
            async with self._tx() as s:
                await self.messages.add(assistant_msg, sa_session=s)
                task = await self._transition(
                    s,
                    task.model_copy(update={"result": task_result}),
                    TaskState.COMPLETED,
                    current_step="persist",
                )
                # Stamp last_surfaced_at + audit commitment.surfaced in the
                # SAME transaction as the response persist (D42): the charge
                # is only "done" if the response that carries it persisted.
                await self.commitment_service.mark_surfaced(
                    [c.id for c in snapshot.commitments_due],
                    now=self.clock.now(),
                    session_id=session_id,
                    task_id=task.id,
                    correlation_id=correlation_id,
                    sa_session=s,
                )
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.TASK_COMPLETED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.SUCCESS,
                    result_summary=(f"status={response.status.value} source={plan_source.value}"),
                    result_digest=_digest(response.model_dump(mode="json")),
                )
            return response

        except asyncio.CancelledError:
            await self._record_cancellation(task, session_id, correlation_id)
            raise JarvisException(
                _error(
                    ErrorCode.TASK_CANCELLED,
                    ErrorCategory.CANCELLATION,
                    "task.cancelled",
                    correlation_id,
                )
            ) from None
        except JarvisException:
            raise
        except TaskConflictError as exc:
            # A concurrent writer won; our view of the task is stale.
            # Never write on top of it.
            raise JarvisException(
                _error(
                    ErrorCode.TASK_CONFLICT,
                    ErrorCategory.CANCELLATION,
                    "task.conflict",
                    correlation_id,
                    retryable=True,
                )
            ) from exc
        except Exception as exc:  # bug guard: never leak internals
            error = _error(
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "internal.error",
                correlation_id,
            )
            async with self._tx() as s:
                try:
                    await self._fail_task(s, task, error, session_id=session_id)
                except Exception:  # noqa: S110 - bug guard already raised
                    pass
            raise JarvisException(error) from exc
        finally:
            self._cancel_events.pop(task.id, None)

    # -- memory slice (Phase 2) ------------------------------------------------

    async def _handle_memory_write(
        self,
        task: TaskRecord,
        user_msg: UserMessage,
        *,
        session_id: str,
        correlation_id: str,
    ) -> AssistantResponse:
        """Explicit memory-write command: parse -> policy -> secret scan ->
        persist, all inside MemoryService. The orchestrator only plans, fails
        and renders."""
        plan = TaskPlan(
            task_id=task.id,
            steps=list(_MEMORY_WRITE_PLAN_STEPS),
            capability_set=[Capability.MEMORY_WRITE.value],
            deadline_at=self.clock.now() + timedelta(seconds=5),
        )
        async with self._tx() as s:
            task = await self._transition(
                s,
                task.model_copy(update={"plan": plan}),
                TaskState.PLANNING,
                current_step="recognize_intent",
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_PLANNED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
            )

        parsed = parse_memory_write(user_msg.content)
        if parsed is None:
            error = _error(
                ErrorCode.INPUT_INVALID,
                ErrorCategory.VALIDATION,
                "memory.unparseable",
                correlation_id,
                source=ErrorSource.USER,
            )
            async with self._tx() as s:
                await self._fail_task(
                    s, task, error, session_id=session_id, current_step="recognize_intent"
                )
            raise JarvisException(error)

        self._raise_if_cancelled(task.id)
        kind, title, content = parsed
        async with self._tx() as s:
            task = await self._transition(s, task, TaskState.RUNNING, current_step="persist_memory")
        try:
            item = await self.memory_service.create(
                kind=kind,
                title=title,
                content=content,
                session_id=session_id,
                source_message_id=user_msg.id,
                task_id=task.id,
                correlation_id=correlation_id,
            )
        except JarvisException as exc:
            # Policy denial, secret block, or invalid args: task failed, audited.
            async with self._tx() as s:
                await self._fail_task(
                    s, task, exc.error, session_id=session_id, current_step="persist_memory"
                )
            raise

        message = render_memory_write_response(item)
        assistant_msg = UserMessage(
            session_id=session_id,
            task_id=task.id,
            role=MessageRole.ASSISTANT,
            content=message,
        )
        task_result = TaskResult(
            outcome=TaskOutcome.SUCCESS,
            response_status=ResponseStatus.NORMAL,
            assistant_message_id=assistant_msg.id,
        )
        async with self._tx() as s:
            await self.messages.add(assistant_msg, sa_session=s)
            task = await self._transition(
                s,
                task.model_copy(update={"result": task_result}),
                TaskState.COMPLETED,
                current_step="persist",
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_COMPLETED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
                result_summary=f"memory.write ok id={item.id}",
            )
        return AssistantResponse(
            task_id=task.id,
            status=ResponseStatus.NORMAL,
            message=message,
            source=ResponseSource.DETERMINISTIC_FALLBACK,
            fact_ids=[],
        )

    async def _handle_memory_read(
        self,
        task: TaskRecord,
        user_msg: UserMessage,
        *,
        session_id: str,
        correlation_id: str,
    ) -> AssistantResponse:
        """Explicit memory-read command: parse -> policy -> read-only
        retrieval. Never mutates state, never touches NEXUS or the LLM."""
        plan = TaskPlan(
            task_id=task.id,
            steps=list(_MEMORY_READ_PLAN_STEPS),
            capability_set=[Capability.MEMORY_READ.value],
            deadline_at=self.clock.now() + timedelta(seconds=5),
        )
        async with self._tx() as s:
            task = await self._transition(
                s,
                task.model_copy(update={"plan": plan}),
                TaskState.PLANNING,
                current_step="recognize_intent",
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_PLANNED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
            )

        query_text = parse_memory_read(user_msg.content)
        if query_text is None:  # pragma: no cover - intent matched, prefix exists
            error = _error(
                ErrorCode.INPUT_INVALID,
                ErrorCategory.VALIDATION,
                "memory.unparseable",
                correlation_id,
                source=ErrorSource.USER,
            )
            async with self._tx() as s:
                await self._fail_task(
                    s, task, error, session_id=session_id, current_step="recognize_intent"
                )
            raise JarvisException(error)

        self._raise_if_cancelled(task.id)
        async with self._tx() as s:
            task = await self._transition(
                s, task, TaskState.RUNNING, current_step="retrieve_memory"
            )
        try:
            if query_text:
                read_result = await self.memory_service.read(
                    MemoryQuery(query=query_text, limit=10),
                    session_id=session_id,
                    task_id=task.id,
                    correlation_id=correlation_id,
                )
                hits = list(read_result.hits)
            else:
                items = await self.memory_service.list_authorized(
                    statuses=frozenset({MemoryStatus.ACTIVE}),
                    limit=10,
                    session_id=session_id,
                    task_id=task.id,
                    correlation_id=correlation_id,
                )
                hits = [
                    MemoryHit(item=item, rank=0.0, snippet=item.content[:200]) for item in items
                ]
        except JarvisException as exc:
            async with self._tx() as s:
                await self._fail_task(
                    s, task, exc.error, session_id=session_id, current_step="retrieve_memory"
                )
            raise

        message = render_memory_read_response(query_text, hits)
        assistant_msg = UserMessage(
            session_id=session_id,
            task_id=task.id,
            role=MessageRole.ASSISTANT,
            content=message,
        )
        task_result = TaskResult(
            outcome=TaskOutcome.SUCCESS,
            response_status=ResponseStatus.NORMAL,
            assistant_message_id=assistant_msg.id,
        )
        async with self._tx() as s:
            await self.messages.add(assistant_msg, sa_session=s)
            task = await self._transition(
                s,
                task.model_copy(update={"result": task_result}),
                TaskState.COMPLETED,
                current_step="persist",
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_COMPLETED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
                result_summary=f"memory.read ok hits={len(hits)}",
            )
        return AssistantResponse(
            task_id=task.id,
            status=ResponseStatus.NORMAL,
            message=message,
            source=ResponseSource.DETERMINISTIC_FALLBACK,
            fact_ids=[],
        )

    # -- commitment slice (Phase 2, Slice 3) -----------------------------------

    async def _plan_commitment_task(
        self,
        task: TaskRecord,
        steps: tuple[str, ...],
        session_id: str,
        correlation_id: str,
    ) -> TaskRecord:
        plan = TaskPlan(
            task_id=task.id,
            steps=list(steps),
            # Commitments do NOT go through policy.decide (D40): the plan
            # exercises no policy capability. The origin gate in
            # CommitmentService is the authorization mechanism.
            capability_set=["none"],
            deadline_at=self.clock.now() + timedelta(seconds=5),
        )
        async with self._tx() as s:
            task = await self._transition(
                s,
                task.model_copy(update={"plan": plan}),
                TaskState.PLANNING,
                current_step="recognize_intent",
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_PLANNED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
            )
        return task

    async def _fail_commitment_task(
        self,
        task: TaskRecord,
        code: ErrorCode,
        key: str,
        session_id: str,
        correlation_id: str,
        current_step: str,
    ) -> JarvisException:
        error = _error(code, ErrorCategory.VALIDATION, key, correlation_id, source=ErrorSource.USER)
        async with self._tx() as s:
            await self._fail_task(s, task, error, session_id=session_id, current_step=current_step)
        return JarvisException(error)

    async def _complete_commitment_task(
        self,
        task: TaskRecord,
        message: str,
        session_id: str,
        correlation_id: str,
        result_summary: str,
    ) -> AssistantResponse:
        assistant_msg = UserMessage(
            session_id=session_id,
            task_id=task.id,
            role=MessageRole.ASSISTANT,
            content=message,
        )
        task_result = TaskResult(
            outcome=TaskOutcome.SUCCESS,
            response_status=ResponseStatus.NORMAL,
            assistant_message_id=assistant_msg.id,
        )
        async with self._tx() as s:
            await self.messages.add(assistant_msg, sa_session=s)
            task = await self._transition(
                s,
                task.model_copy(update={"result": task_result}),
                TaskState.COMPLETED,
                current_step="persist",
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_COMPLETED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
                result_summary=result_summary,
            )
        return AssistantResponse(
            task_id=task.id,
            status=ResponseStatus.NORMAL,
            message=message,
            source=ResponseSource.DETERMINISTIC_FALLBACK,
            fact_ids=[],
        )

    async def _handle_commitment_create(
        self,
        task: TaskRecord,
        user_msg: UserMessage,
        *,
        session_id: str,
        correlation_id: str,
    ) -> AssistantResponse:
        """Explicit charge command: parse -> origin-gated create -> render."""
        task = await self._plan_commitment_task(
            task, _COMMITMENT_CREATE_PLAN_STEPS, session_id, correlation_id
        )

        parsed = parse_commitment_create(user_msg.content, self.clock.now())
        if parsed is None:
            raise await self._fail_commitment_task(
                task,
                ErrorCode.INPUT_INVALID,
                "commitment.unparseable",
                session_id,
                correlation_id,
                current_step="recognize_intent",
            )

        self._raise_if_cancelled(task.id)
        title, due_at = parsed
        async with self._tx() as s:
            task = await self._transition(
                s, task, TaskState.RUNNING, current_step="create_commitment"
            )
        try:
            commitment = await self.commitment_service.create(
                title=title,
                due_at=due_at,
                origin=USER_EXPLICIT_ORIGIN,
                session_id=session_id,
                source_message_id=user_msg.id,
                task_id=task.id,
                correlation_id=correlation_id,
            )
        except JarvisException as exc:
            async with self._tx() as s:
                await self._fail_task(
                    s, task, exc.error, session_id=session_id, current_step="create_commitment"
                )
            raise

        return await self._complete_commitment_task(
            task,
            render_commitment_create_response(commitment),
            session_id,
            correlation_id,
            f"commitment.create ok id={commitment.id}",
        )

    async def _handle_commitment_fulfill(
        self,
        task: TaskRecord,
        user_msg: UserMessage,
        *,
        session_id: str,
        correlation_id: str,
    ) -> AssistantResponse:
        """Explicit fulfillment: parse -> match ONE open commitment ->
        origin-gated fulfill -> render."""
        task = await self._plan_commitment_task(
            task, _COMMITMENT_FULFILL_PLAN_STEPS, session_id, correlation_id
        )

        match_text = parse_commitment_fulfill(user_msg.content)
        if match_text is None:
            raise await self._fail_commitment_task(
                task,
                ErrorCode.INPUT_INVALID,
                "commitment.unparseable",
                session_id,
                correlation_id,
                current_step="recognize_intent",
            )

        self._raise_if_cancelled(task.id)
        async with self._tx() as s:
            task = await self._transition(
                s, task, TaskState.RUNNING, current_step="match_commitment"
            )
        open_items = await self.commitment_service.list_commitments(
            statuses=frozenset({CommitmentStatus.OPEN}), limit=50
        )
        needle = _normalize(match_text)
        candidates = [c for c in open_items if needle and needle in _normalize(c.title)]
        if not candidates:
            raise await self._fail_commitment_task(
                task,
                ErrorCode.NOT_FOUND,
                "commitment.no_match",
                session_id,
                correlation_id,
                current_step="match_commitment",
            )
        if len(candidates) > 1:
            raise await self._fail_commitment_task(
                task,
                ErrorCode.INPUT_INVALID,
                "commitment.ambiguous",
                session_id,
                correlation_id,
                current_step="match_commitment",
            )
        target = candidates[0]

        # Already RUNNING (matched above); the plan's fulfill step runs here.
        self._raise_if_cancelled(task.id)
        try:
            commitment = await self.commitment_service.fulfill(
                target.id,
                origin=USER_EXPLICIT_ORIGIN,
                session_id=session_id,
                task_id=task.id,
                correlation_id=correlation_id,
            )
        except JarvisException as exc:
            async with self._tx() as s:
                await self._fail_task(
                    s, task, exc.error, session_id=session_id, current_step="fulfill_commitment"
                )
            raise

        return await self._complete_commitment_task(
            task,
            render_commitment_fulfill_response(commitment),
            session_id,
            correlation_id,
            f"commitment.fulfill ok id={commitment.id}",
        )

    async def _replay_if_exists(
        self, session_id: str, idempotency_key: str
    ) -> AssistantResponse | None:
        """Return the original response for a repeated idempotency key."""
        existing = await self.messages.find_by_idempotency_key(session_id, idempotency_key)
        if existing is None or not existing.task_id:
            return None
        prior = await self.messages.list_assistant_by_task(existing.task_id)
        if not prior:
            return None
        return await self._assistant_response_from_stored(existing.task_id, prior[0])

    async def _assistant_response_from_stored(
        self, task_id: str, stored: UserMessage
    ) -> AssistantResponse:
        task = await self.tasks.get(task_id)
        status = ResponseStatus.NORMAL
        source = ResponseSource.DETERMINISTIC_FALLBACK
        if task is not None and task.result is not None:
            if task.result.response_status is not None:
                status = task.result.response_status
        events = await self.audit.list_by_task(task_id)
        if any(e.event_type is AuditEventType.LLM_COMPLETED for e in events):
            source = ResponseSource.LLM_PLAN
        return AssistantResponse(
            task_id=task_id,
            status=status,
            message=stored.content,
            source=source,
            fact_ids=[],
        )

    # -- context assembly (Phase 2, Slice 2) --------------------------------

    def _ctx_budgets(self) -> ContextBudgets:
        return ContextBudgets(
            total=self.settings.ctx_total_chars,
            conversation_tail=self.settings.ctx_tail_chars,
            preferences=self.settings.ctx_preferences_chars,
            memories=self.settings.ctx_memories_chars,
            nexus_facts=self.settings.ctx_facts_chars,
        )

    async def _assemble_context(
        self,
        task_id: str,
        session_id: str,
        content: str,
        verified: VerifiedNexusStatus,
    ) -> ContextSnapshot:
        """Fetch the context inputs and build the immutable snapshot.

        Retrieval here is the orchestrator's own read-only path
        (memory_service.retrieve, never the authorized tool path): context
        assembly is not a tool invocation. The pure ContextBuilder then
        shapes the data; it never executes tools (D38).
        """
        query = MemoryQuery(
            query=content[:500],
            kinds=frozenset({MemoryKind.PREFERENCE, MemoryKind.FACT, MemoryKind.PROJECT_NOTE}),
            limit=5,
        )
        hits = await self.memory_service.retrieve(query)
        preference_hits = tuple(h for h in hits if h.item.kind is MemoryKind.PREFERENCE)
        memory_hits = tuple(h for h in hits if h.item.kind is not MemoryKind.PREFERENCE)

        envelope = await self.messages.list_by_session(session_id, limit=100)
        tail_messages = tuple(envelope)

        commitments_due = tuple(await self._commitment_notes())

        return self.context_builder.build(
            ContextBuildInput(
                verified=verified,
                memory_hits=memory_hits,
                preference_hits=preference_hits,
                tail_messages=tail_messages,
                commitments_due=commitments_due,
            ),
            task_id=task_id,
            session_id=session_id,
            budgets=self._ctx_budgets(),
            now=self.clock.now(),
        )

    async def _commitment_notes(self) -> list[CommitmentCtx]:
        """Commitments due for in-conversation surfacing (§6.4).

        Read-only. Presentation only: this never executes any external
        action — the renderer just appends the deterministic "📌 Lembretes"
        block. last_surfaced_at is stamped by mark_surfaced in the response
        persist transaction (12h dedup).
        """
        now = self.clock.now()
        due = await self.commitment_service.due_for_surfacing(now=now)
        return [
            CommitmentCtx(
                id=c.id,
                title=c.title,
                due_at=c.due_at,
                overdue=c.due_at is not None and c.due_at <= now,
            )
            for c in due
        ]

    async def _finish_without_llm(
        self,
        task: TaskRecord,
        verified: VerifiedNexusStatus,
        session_id: str,
        correlation_id: str,
    ) -> AssistantResponse:
        """NEXUS unavailable: deterministic answer, COMPLETED/UNAVAILABLE."""
        plan = build_fallback_plan(verified, task_id=task.id)
        commitment_notes = await self._commitment_notes()
        response = render_assistant_response(
            verified,
            plan,
            task_id=task.id,
            source=ResponseSource.DETERMINISTIC_FALLBACK,
            commitment_notes=commitment_notes,
        )
        assistant_msg = UserMessage(
            session_id=session_id,
            task_id=task.id,
            role=MessageRole.ASSISTANT,
            content=response.message,
        )
        task_result = TaskResult(
            outcome=TaskOutcome.UNAVAILABLE,
            response_status=response.status,
            assistant_message_id=assistant_msg.id,
        )
        async with self._tx() as s:
            await self.messages.add(assistant_msg, sa_session=s)
            task = await self._transition(
                s,
                task.model_copy(update={"result": task_result}),
                TaskState.COMPLETED,
                current_step="render_response",
            )
            await self.commitment_service.mark_surfaced(
                [c.id for c in commitment_notes],
                now=self.clock.now(),
                session_id=session_id,
                task_id=task.id,
                correlation_id=correlation_id,
                sa_session=s,
            )
            await self._audit(
                s,
                session_id=session_id,
                task_id=task.id,
                event=AuditEventType.TASK_COMPLETED,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
                result_summary=(
                    f"status={response.status.value} "
                    "source=deterministic_fallback (nexus unavailable)"
                ),
                result_digest=_digest(response.model_dump(mode="json")),
            )
        return response

    async def _record_cancellation(
        self, task: TaskRecord, session_id: str, correlation_id: str
    ) -> None:
        try:
            async with self._tx() as s:
                fresh = await self.tasks.get(task.id)
                if fresh is None or fresh.state in (
                    TaskState.COMPLETED,
                    TaskState.FAILED,
                    TaskState.CANCELLED,
                ):
                    return
                moved = transition(
                    fresh.model_copy(update={"error_code": ErrorCode.TASK_CANCELLED}),
                    TaskState.CANCELLED,
                )
                await self.tasks.update(moved, expected_version=fresh.version, sa_session=s)
                await self._audit(
                    s,
                    session_id=session_id,
                    task_id=task.id,
                    event=AuditEventType.TASK_CANCELLED,
                    correlation_id=correlation_id,
                    outcome=AuditOutcome.CANCELLED,
                    error_code=ErrorCode.TASK_CANCELLED,
                )
        except Exception:  # noqa: S110 - cancellation bookkeeping is best-effort
            pass
