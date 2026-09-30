"""Commitment application service (Phase 2, Slice 3).

Commitments are obligations the user asked to be charged about ("me cobre
de ..."). They are NOT tasks: the task state machine never applies to them
and they never become tasks on their own.

Lifecycle (closed): open -> fulfilled / expired / cancelled.
- FULFILLED / CANCELLED: only by explicit user command (chat intent or
  loopback endpoint), via the origin gate below.
- EXPIRED: only by the idempotent sweep (open + due_at <= now).
Terminal states are immutable: any transition out of fulfilled/expired/
cancelled is rejected, including under races (check-and-set).

Authorization (D40): commitments do NOT go through policy.decide — the
review's security model (§9.1) enumerates exactly three capabilities and
adding new ones would expand the approved policy surface. Instead every
mutating entry point requires origin == "user_explicit_command"
(the same literal the memory-write rule uses), intents are recognized
only from user text, and the HTTP endpoints are loopback-only. The LLM,
tool outputs and NEXUS content can never construct a commitment op.

Surfacing (cobrança) is presentation inside the conversation ONLY:
- due_for_surfacing(): open commitments, due_at <= now+24h, not surfaced
  in the last 12h (last_surfaced_at dedup). Read-only.
- mark_surfaced(): stamps last_surfaced_at + audits commitment.surfaced,
  in the caller's transaction.
ABSOLUTE RULE: surfacing never executes an external action — no webhook,
no NEXUS call, no Clone Cobrador call, no outbound message, no automatic
task creation. T18 proves this with exploding mocks.

The sweep also expires memory items whose valid_until passed (§6.1),
auditing memory.expired per item. Cooldown (1h) is persisted in
service_meta so it survives restarts without a scheduler.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.persistence.database import Database
from ..adapters.persistence.repositories import (
    SqlAuditRepository,
    SqlCommitmentRepository,
    SqlMemoryRepository,
    SqlServiceMetaRepository,
)
from ..domain.contracts.audit import AuditActor, AuditEventType, AuditLog, AuditOutcome
from ..domain.contracts.commitments import (
    Commitment,
    CommitmentStatus,
    SweepResult,
    transition_commitment,
)
from ..domain.contracts.common import new_id
from ..domain.errors import ErrorCategory, ErrorCode, ErrorSource, JarvisError, JarvisException
from ..ports.clock import Clock, SystemClock
from ..ports.commitments import CommitmentSource
from ..security.memory_safety import scan_for_secrets

log = logging.getLogger(__name__)

#: service_meta key holding the last sweep timestamp (ISO-8601).
LAST_SWEEP_KEY = "commitments.last_sweep_at"

#: Minimum interval between non-forced sweep passes.
SWEEP_COOLDOWN = timedelta(hours=1)

#: A commitment surfaces when due within this horizon (overdue included).
SURFACE_HORIZON = timedelta(hours=24)

#: The same commitment is not charged twice within this window.
SURFACE_DEDUP = timedelta(hours=12)

#: Max rows expired per sweep pass (commitments and memories each).
SWEEP_BATCH_LIMIT = 500

#: Max commitments surfaced per interaction (keeps the 📌 block small;
#: the snapshot section budget is 1000 chars, never truncated silently).
SURFACE_LIMIT = 10

#: The only accepted origin for commitment mutations.
USER_EXPLICIT_ORIGIN: Literal["user_explicit_command"] = "user_explicit_command"


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


def _normalize(text: str) -> str:
    lowered = text.lower()
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9/ ]", " ", stripped)


_WEEKDAY_BY_NAME = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
}

_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*$")


def _end_of_day_utc(day: date) -> datetime:
    """Due dates are end-of-day UTC (D41). Naive local-day semantics would
    need a user timezone the system does not have yet."""
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def parse_due_date(expr: str, now: AwareDatetime) -> AwareDatetime | None:
    """Deterministic pt-BR due-date parser. Pure, no I/O.

    Understands: "hoje", "amanhã", weekday names ("sexta", "sexta-feira"),
    and "dd/mm"[/yyyy]. Returns end-of-day UTC, or None when the expression
    is not understood (the caller then keeps the text as title, due_at=None).
    """
    text = _normalize(expr).strip()
    if not text:
        return None
    base = now.astimezone(UTC).date()
    target: date | None = None
    if text == "amanha":
        target = base + timedelta(days=1)
    elif text == "hoje":
        target = base
    else:
        weekday_name = text[:-6].strip() if text.endswith(" feira") else text
        weekday = _WEEKDAY_BY_NAME.get(weekday_name)
        if weekday is not None:
            delta = (weekday - base.weekday()) % 7
            target = base + timedelta(days=delta)
        else:
            m = _DATE_RE.match(text)
            if m:
                day, month = int(m.group(1)), int(m.group(2))
                year_group = m.group(3)
                if year_group is None:
                    year = base.year
                    try:
                        candidate = date(year, month, day)
                    except ValueError:
                        return None
                    target = candidate if candidate >= base else date(year + 1, month, day)
                else:
                    year = int(year_group)
                    year += 2000 if year < 100 else 0
                    try:
                        target = date(year, month, day)
                    except ValueError:
                        return None
    if target is None:
        return None
    return _end_of_day_utc(target)


_DUE_TAIL_RE = re.compile(
    r"\s+(?:at[eé]|para|antes\s+de|no\s+dia)\s+(.+?)\s*$",
    re.IGNORECASE,
)


def split_due_expression(text: str, now: AwareDatetime) -> tuple[str, AwareDatetime | None]:
    """Split a trailing due expression ("... até amanhã") from the title.

    Returns (title, due_at). When the trailing expression is not a parsable
    date it stays part of the title and due_at is None — deterministic, no
    guessing.
    """
    m = _DUE_TAIL_RE.search(text)
    if not m:
        return text.strip(), None
    due_at = parse_due_date(m.group(1), now)
    if due_at is None:
        return text.strip(), None
    title = text[: m.start()].strip()
    return (title if title else text.strip()), due_at


@dataclass
class CommitmentService:
    db: Database
    commitments: SqlCommitmentRepository
    memory: SqlMemoryRepository
    audit: SqlAuditRepository
    meta: SqlServiceMetaRepository
    source: CommitmentSource | None = None
    clock: Clock = field(default_factory=SystemClock)

    def __post_init__(self) -> None:
        if self.source is None:
            # Local source is the only one in Phase 2; the sweep lists its
            # candidates through the port so a future external source plugs
            # in without touching this service.
            from ..adapters.commitments.local import LocalCommitmentSource

            self.source = LocalCommitmentSource(self.commitments)

    # -- transactions -----------------------------------------------------

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self.db.session() as s, s.begin():
            yield s

    async def _audit(
        self,
        s: AsyncSession,
        *,
        event: AuditEventType,
        correlation_id: str,
        session_id: str | None,
        task_id: str | None,
        actor: AuditActor,
        outcome: AuditOutcome,
        request_summary: str | None = None,
        result_summary: str | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        # ids are random UUIDs, not sensitive; titles/due details stay out of
        # the audit log (redaction). Ids travel in result_summary only.
        await self.audit.append(
            AuditLog(
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                event_type=event,
                actor=actor,
                request_summary=request_summary,
                result_summary=result_summary,
                error_code=error_code,
                outcome=outcome,
            ),
            sa_session=s,
        )

    def _require_origin(self, origin: str, correlation_id: str) -> None:
        if origin != USER_EXPLICIT_ORIGIN:
            raise JarvisException(
                _error(
                    ErrorCode.POLICY_DENIED,
                    "commitment.origin_denied",
                    correlation_id,
                    category=ErrorCategory.POLICY,
                    source=ErrorSource.POLICY,
                )
            )

    async def _scan_or_block(
        self,
        *,
        title: str,
        detail: str | None,
        session_id: str | None,
        task_id: str | None,
        correlation_id: str,
    ) -> None:
        """Pre-persistence secret scan (F1 hardening).

        Reuses the same scanner as memory writes: title and detail are
        scanned BEFORE anything is persisted. A hit denies the write:
        nothing is stored (and therefore nothing reaches the FTS index,
        the conversation tail, the context snapshot or the LLM provider),
        the audit entry carries categories only — never the secret value —
        and it is committed in its own transaction so the block survives
        the rollback of the persistence transaction that never happened
        (D31 property preserved).

        The scan runs before the origin gate on purpose: it is a pure,
        side-effect-free check and the block must hold regardless of
        origin validity.

        No second defense is added in the ContextBuilder: with the block
        at the write path there is no legitimate route for a commitment
        secret to reach stored state, and commitments have no legacy
        data (table introduced in migration 0003, no import path).
        """
        hits = scan_for_secrets(f"{title}\n{detail or ''}")
        if not hits:
            return
        categories = ",".join(hit.category for hit in hits)
        async with self._tx() as s:
            await self._audit(
                s,
                event=AuditEventType.COMMITMENT_WRITE_BLOCKED,
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                # No capability/tool_name: commitments do not go through
                # policy.decide (D40); the origin gate below is the
                # authorization step.
                actor=AuditActor.LOCAL_USER,
                outcome=AuditOutcome.DENIED,
                request_summary=(
                    f"commitment.create title_len={len(title)} "
                    f"detail_len={len(detail) if detail else 0}"
                ),
                # Categories only — the secret VALUES never reach the audit log.
                result_summary=f"secret blocked: categories={categories}",
                error_code=ErrorCode.COMMITMENT_SECRET_DETECTED,
            )
        raise JarvisException(
            _error(
                ErrorCode.COMMITMENT_SECRET_DETECTED,
                "commitment.secret_detected",
                correlation_id,
                category=ErrorCategory.POLICY,
            )
        )

    # -- mutations (explicit user command only) ----------------------------

    async def create(
        self,
        *,
        title: str,
        detail: str | None = None,
        due_at: AwareDatetime | None = None,
        origin: str,
        session_id: str | None = None,
        source_message_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> Commitment:
        """Create an open commitment. Only from an explicit user command.

        Secret scan runs before the origin gate and before persistence:
        a commitment carrying a secret is never stored.
        """
        await self._scan_or_block(
            title=title,
            detail=detail,
            session_id=session_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        self._require_origin(origin, correlation_id)
        now = self.clock.now()
        commitment = Commitment(
            title=title,
            detail=detail or None,
            due_at=due_at,
            status=CommitmentStatus.OPEN,
            created_by="local_user",
            source_message_id=source_message_id,
            created_at=now,
            updated_at=now,
        )
        async with self._tx() as s:
            await self.commitments.create(commitment, sa_session=s)
            await self._audit(
                s,
                event=AuditEventType.COMMITMENT_CREATED,
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                actor=AuditActor.LOCAL_USER,
                outcome=AuditOutcome.SUCCESS,
                # Lengths and dates only: raw titles never reach the audit log.
                request_summary=(
                    f"commitment.create title_len={len(title)} "
                    f"due_at={due_at.isoformat() if due_at else '-'}"
                ),
                result_summary=f"id={commitment.id} status=open",
            )
        return commitment

    async def _get_or_raise(self, commitment_id: str, correlation_id: str) -> Commitment:
        commitment = await self.commitments.get(commitment_id)
        if commitment is None:
            raise JarvisException(
                _error(ErrorCode.NOT_FOUND, "commitment.not_found", correlation_id)
            )
        return commitment

    def _illegal_transition(self, correlation_id: str) -> JarvisException:
        return JarvisException(
            _error(
                ErrorCode.INPUT_INVALID,
                "commitment.invalid_transition",
                correlation_id,
                category=ErrorCategory.VALIDATION,
                source=ErrorSource.USER,
            )
        )

    async def fulfill(
        self,
        commitment_id: str,
        *,
        origin: str,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> Commitment:
        """Mark an open commitment fulfilled. Terminal states reject."""
        self._require_origin(origin, correlation_id)
        current = await self._get_or_raise(commitment_id, correlation_id)
        try:
            transition_commitment(current.status, CommitmentStatus.FULFILLED)
        except ValueError:
            raise self._illegal_transition(correlation_id) from None
        now = self.clock.now()
        async with self._tx() as s:
            updated = await self.commitments.set_status(
                commitment_id,
                CommitmentStatus.FULFILLED,
                expected_from=frozenset({CommitmentStatus.OPEN}),
                fulfilled_at=now,
                sa_session=s,
            )
            if updated is None:  # lost a race; the row moved under us
                raise self._illegal_transition(correlation_id)
            await self._audit(
                s,
                event=AuditEventType.COMMITMENT_FULFILLED,
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                actor=AuditActor.LOCAL_USER,
                outcome=AuditOutcome.SUCCESS,
                result_summary=f"id={commitment_id} status=fulfilled",
            )
        return updated

    async def cancel(
        self,
        commitment_id: str,
        *,
        origin: str,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str,
    ) -> Commitment:
        """Cancel an open commitment. Terminal states reject."""
        self._require_origin(origin, correlation_id)
        current = await self._get_or_raise(commitment_id, correlation_id)
        try:
            transition_commitment(current.status, CommitmentStatus.CANCELLED)
        except ValueError:
            raise self._illegal_transition(correlation_id) from None
        async with self._tx() as s:
            updated = await self.commitments.set_status(
                commitment_id,
                CommitmentStatus.CANCELLED,
                expected_from=frozenset({CommitmentStatus.OPEN}),
                sa_session=s,
            )
            if updated is None:
                raise self._illegal_transition(correlation_id)
            await self._audit(
                s,
                event=AuditEventType.COMMITMENT_CANCELLED,
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                actor=AuditActor.LOCAL_USER,
                outcome=AuditOutcome.SUCCESS,
                result_summary=f"id={commitment_id} status=cancelled",
            )
        return updated

    # -- reads --------------------------------------------------------------

    async def get(self, commitment_id: str) -> Commitment | None:
        return await self.commitments.get(commitment_id)

    async def list_commitments(
        self,
        *,
        statuses: frozenset[CommitmentStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Commitment]:
        return await self.commitments.list_commitments(
            statuses=statuses, limit=limit, offset=offset
        )

    # -- sweep ---------------------------------------------------------------

    async def sweep(
        self,
        *,
        now: AwareDatetime | None = None,
        force: bool = False,
        session_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SweepResult:
        """Idempotent expiry pass (§6.3).

        Expires open commitments with due_at <= now (due_at NULL never
        expires) and active memories with valid_until <= now, bounded to
        SWEEP_BATCH_LIMIT rows each. The 1h cooldown is persisted in
        service_meta; force=True bypasses it (internal cron route).
        Everything happens in one transaction with its own audit trail.
        """
        now = now or self.clock.now()
        correlation_id = correlation_id or new_id()
        if not force:
            last_raw = await self.meta.get(LAST_SWEEP_KEY)
            if last_raw is not None:
                try:
                    last = datetime.fromisoformat(last_raw)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=UTC)
                    if now - last < SWEEP_COOLDOWN:
                        return SweepResult(ran_at=now, cooldown_skipped=True)
                except ValueError:
                    log.warning(
                        "sweep: unparseable %s=%r; running anyway", LAST_SWEEP_KEY, last_raw
                    )

        assert self.source is not None  # set in __post_init__
        candidates = await self.source.list_open()
        due_ids = [
            c["external_id"] for c in candidates if c["due_at"] is not None and c["due_at"] <= now
        ][:SWEEP_BATCH_LIMIT]

        async with self._tx() as s:
            expired_commitments: list[str] = []
            for cid in due_ids:
                updated = await self.commitments.set_status(
                    cid,
                    CommitmentStatus.EXPIRED,
                    expected_from=frozenset({CommitmentStatus.OPEN}),
                    sa_session=s,
                )
                if updated is not None:
                    expired_commitments.append(cid)
                    await self._audit(
                        s,
                        event=AuditEventType.COMMITMENT_EXPIRED,
                        correlation_id=correlation_id,
                        session_id=session_id,
                        task_id=task_id,
                        actor=AuditActor.SYSTEM,
                        outcome=AuditOutcome.SUCCESS,
                        result_summary=f"id={cid} status=expired",
                    )
            expired_memories = await self.memory.expire_due(now, SWEEP_BATCH_LIMIT, sa_session=s)
            for mid in expired_memories:
                await self._audit(
                    s,
                    event=AuditEventType.MEMORY_EXPIRED,
                    correlation_id=correlation_id,
                    session_id=session_id,
                    task_id=task_id,
                    actor=AuditActor.SYSTEM,
                    outcome=AuditOutcome.SUCCESS,
                    result_summary=f"id={mid} status=expired",
                )
            await self.meta.set(LAST_SWEEP_KEY, now.isoformat(), now, sa_session=s)
            await self._audit(
                s,
                event=AuditEventType.COMMITMENT_SWEEP,
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                actor=AuditActor.SYSTEM,
                outcome=AuditOutcome.SUCCESS,
                result_summary=(
                    f"expired_commitments={len(expired_commitments)} "
                    f"expired_memories={len(expired_memories)}"
                ),
            )
        return SweepResult(
            ran_at=now,
            cooldown_skipped=False,
            expired_commitment_ids=expired_commitments,
            expired_memory_ids=expired_memories,
        )

    # -- surfacing (cobrança in-conversation; presentation only) --------------

    async def due_for_surfacing(
        self, *, now: AwareDatetime | None = None, limit: int = SURFACE_LIMIT
    ) -> list[Commitment]:
        """Open commitments due within 24h not surfaced in the last 12h.
        Read-only. Never triggers any external action."""
        now = now or self.clock.now()
        return await self.commitments.due_for_surfacing(
            now,
            now + SURFACE_HORIZON,
            now - SURFACE_DEDUP,
            limit=limit,
        )

    async def mark_surfaced(
        self,
        commitment_ids: list[str],
        *,
        now: AwareDatetime | None = None,
        session_id: str | None,
        task_id: str | None,
        correlation_id: str,
        sa_session: AsyncSession,
    ) -> None:
        """Stamp last_surfaced_at and audit commitment.surfaced per id, in
        the caller's transaction (the response persist tx)."""
        if not commitment_ids:
            return
        now = now or self.clock.now()
        await self.commitments.mark_surfaced(commitment_ids, now, sa_session=sa_session)
        for cid in commitment_ids:
            await self._audit(
                sa_session,
                event=AuditEventType.COMMITMENT_SURFACED,
                correlation_id=correlation_id,
                session_id=session_id,
                task_id=task_id,
                actor=AuditActor.SYSTEM,
                outcome=AuditOutcome.SUCCESS,
                result_summary=f"id={cid} surfaced",
            )


__all__ = [
    "LAST_SWEEP_KEY",
    "SURFACE_DEDUP",
    "SURFACE_HORIZON",
    "SURFACE_LIMIT",
    "SWEEP_BATCH_LIMIT",
    "SWEEP_COOLDOWN",
    "USER_EXPLICIT_ORIGIN",
    "CommitmentService",
    "parse_due_date",
    "split_due_expression",
]
