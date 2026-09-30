"""JARVIS Phase 1 HTTP API. Local-only: binds 127.0.0.1, port 8123."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, Field

from ..application.commitments import USER_EXPLICIT_ORIGIN
from ..application.recovery import recover_interrupted_tasks
from ..config import SCHEMA_HEAD, Settings, get_settings
from ..domain.contracts.commitments import CommitmentStatus
from ..domain.contracts.common import utcnow
from ..domain.contracts.memory import (
    MEMORY_SCHEMA_VERSION,
    MemoryKind,
    MemoryQuery,
    MemoryStatus,
    Sensitivity,
)
from ..domain.contracts.session import SessionRecord
from ..domain.contracts.task import AssistantResponse
from ..domain.errors import ErrorCategory, ErrorCode, JarvisError, JarvisException
from ..observability.logging import configure_logging, get_logger, set_correlation_id
from ..observability.metrics import METRICS
from .dependencies import AppState, build_app_state, check_readiness

log = get_logger(__name__)

VERSION = "0.1.0"

# F3.1 web shell: single static page served by the JARVIS process itself.
# Same origin as the API; the page only fetches the API (client role only).
_WEB_ROOT = Path(__file__).resolve().parent / "static"

_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INPUT_INVALID: 422,
    ErrorCode.INTENT_UNSUPPORTED: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.POLICY_DENIED: 403,
    ErrorCode.CONFIRMATION_REQUIRED: 409,
    ErrorCode.TASK_CANCELLED: 409,
    ErrorCode.TASK_CONFLICT: 409,
    ErrorCode.MEMORY_SECRET_DETECTED: 422,
    ErrorCode.COMMITMENT_SECRET_DETECTED: 422,
}


def _api_error(
    code: ErrorCode, category: ErrorCategory, message_key: str, correlation_id: str
) -> JarvisException:
    return JarvisException(
        JarvisError(
            code=code,
            category=category,
            user_message_key=message_key,
            correlation_id=correlation_id,
        )
    )


def _error_payload(exc: JarvisException, correlation_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": exc.error.code.value,
            "message_key": exc.error.user_message_key,
            "correlation_id": correlation_id,
        }
    }


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class SessionOut(BaseModel):
    id: str


class CancelOut(BaseModel):
    task_id: str
    cancelled: bool
    state: str | None = None


class ReadyOut(BaseModel):
    ready: bool
    reason: str


class VersionOut(BaseModel):
    version: str
    schema_version: str
    policy_version: str
    memory_schema_version: str


class MemorySupersedeIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sensitivity: str | None = Field(default=None, pattern="^(standard|sensitive)$")


class CommitmentCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=2000)
    due_at: AwareDatetime | None = None


def _cid(request: Request) -> str:
    return getattr(request.state, "correlation_id", "-")


def _commitment_status(value: str | None, cid: str) -> frozenset[CommitmentStatus] | None:
    if value is None:
        return None
    try:
        return frozenset({CommitmentStatus(value)})
    except ValueError:
        raise _api_error(
            ErrorCode.INPUT_INVALID, ErrorCategory.VALIDATION, "commitment.invalid_args", cid
        ) from None


def _memory_kind(value: str | None, cid: str) -> frozenset[MemoryKind] | None:
    if value is None:
        return None
    try:
        return frozenset({MemoryKind(value)})
    except ValueError:
        raise _api_error(
            ErrorCode.INPUT_INVALID, ErrorCategory.VALIDATION, "memory.invalid_args", cid
        ) from None


def _memory_status(value: str | None, cid: str) -> frozenset[MemoryStatus] | None:
    if value is None:
        return None
    try:
        return frozenset({MemoryStatus(value)})
    except ValueError:
        raise _api_error(
            ErrorCode.INPUT_INVALID, ErrorCategory.VALIDATION, "memory.invalid_args", cid
        ) from None


def _state(app: FastAPI) -> AppState:
    return app.state.jarvis  # type: ignore[no-any-return]


async def _get_state(request: Request) -> AppState:
    return _state(request.app)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = app.state.jarvis
    ok, reason = await check_readiness(state)
    if not ok:
        log.warning("startup: not ready (%s)", reason)
    else:
        log.info("startup: ready (schema=%s)", SCHEMA_HEAD)
        # Crash recovery: never leave a task non-terminal after a restart.
        # Runs only when the schema is at head (the tables are trustworthy).
        recovered = await recover_interrupted_tasks(
            state.tasks, state.audit, correlation_id=uuid.uuid4().hex[:16]
        )
        if recovered:
            log.warning("startup: recovered %d interrupted task(s)", recovered)
    try:
        yield
    finally:
        await state.orchestrator.nexus.close()
        await state.db.close()
        log.info("shutdown: resources released")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(title="JARVIS", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    # Built eagerly (not in lifespan) so the app also works under ASGI
    # transports that never run lifespan (tests, some embeddings).
    app.state.jarvis = build_app_state(settings)

    # -- error handling ----------------------------------------------------

    @app.exception_handler(JarvisException)
    async def _jarvis_error(request: Request, exc: JarvisException) -> JSONResponse:
        status = _STATUS_BY_CODE.get(exc.error.code, 500)
        cid = getattr(request.state, "correlation_id", "-")
        log.warning("request failed: %s (%s)", exc.error.code.value, exc.error.user_message_key)
        return JSONResponse(_error_payload(exc, cid), status_code=status)

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        cid = getattr(request.state, "correlation_id", "-")
        log.exception("unexpected error")
        return JSONResponse(
            {
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message_key": "internal_error",
                    "correlation_id": cid,
                }
            },
            status_code=500,
        )

    # -- middleware: correlation id + access log + metrics -----------------

    @app.middleware("http")
    async def _request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        cid = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex[:16]
        request.state.correlation_id = cid
        set_correlation_id(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            set_correlation_id("-")
        elapsed_ms = (time.perf_counter() - start) * 1000
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        METRICS.inc(
            "http_requests_total",
            method=request.method,
            path=template,
            status=str(response.status_code),
        )
        log.info(
            "%s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        response.headers["X-Correlation-Id"] = cid
        return response

    # -- web shell (F3.1) -----------------------------------------------------
    # Thin presentation layer only: serves the static page. No business
    # logic, no policy, no keys; the page talks back only to this API.

    @app.get("/", include_in_schema=False)
    async def web_shell() -> FileResponse:
        return FileResponse(_WEB_ROOT / "index.html", media_type="text/html")

    # -- operational endpoints ---------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    @app.get("/ready", response_model=ReadyOut)
    async def ready(request: Request) -> JSONResponse:
        ok, reason = await check_readiness(_state(request.app))
        return JSONResponse({"ready": ok, "reason": reason}, status_code=200 if ok else 503)

    @app.get("/version", response_model=VersionOut)
    async def version(request: Request) -> dict[str, str]:
        st = _state(request.app)
        return {
            "version": VERSION,
            "schema_version": SCHEMA_HEAD,
            "policy_version": st.policy.policy_version,
            "memory_schema_version": str(MEMORY_SCHEMA_VERSION),
        }

    # -- API v1 ------------------------------------------------------------

    @app.post("/api/v1/sessions", response_model=SessionOut, status_code=201)
    async def create_session(state: AppState = Depends(_get_state)) -> dict[str, str]:
        record = SessionRecord()
        await state.sessions.create(record)
        return {"id": record.id}

    @app.post("/api/v1/sessions/{session_id}/messages")
    async def send_message(
        session_id: str,
        body: MessageIn,
        state: AppState = Depends(_get_state),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        response: AssistantResponse = await state.orchestrator.handle_message(
            session_id=session_id,
            content=body.content,
            idempotency_key=idempotency_key,
        )
        return JSONResponse(response.model_dump(mode="json"))

    @app.post("/api/v1/tasks/{task_id}/cancel", response_model=CancelOut)
    async def cancel_task(
        task_id: str, request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = getattr(request.state, "correlation_id", "-")
        if state.orchestrator.cancel_task(task_id):
            return JSONResponse({"task_id": task_id, "cancelled": True}, status_code=202)
        task = await state.tasks.get(task_id)
        if task is None:
            raise _api_error(ErrorCode.NOT_FOUND, ErrorCategory.VALIDATION, "task.not_found", cid)
        return JSONResponse({"task_id": task_id, "cancelled": False, "state": task.state.value})

    # -- memory API v1 (Phase 2, Slice 1) ------------------------------------
    # Writes only via explicit user command; the service enforces
    # contract -> policy -> secret-scan -> persist for every op.

    @app.get("/api/v1/memory")
    async def list_memories(
        request: Request,
        state: AppState = Depends(_get_state),
        q: str | None = Query(default=None, max_length=500),
        kind: str | None = Query(default=None, max_length=32),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=20, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        cid = _cid(request)
        kinds = _memory_kind(kind, cid)
        statuses = _memory_status(status, cid)
        if q:
            # FTS search path: fetch limit+offset, then slice (MATCH has no OFFSET).
            query = MemoryQuery(query=q, kinds=kinds, limit=min(limit + offset, 50))
            result = await state.memory.read(query, correlation_id=cid)
            items = [hit.item for hit in result.hits][offset:]
        else:
            items = await state.memory.list_authorized(
                kinds=kinds, statuses=statuses, limit=limit, offset=offset, correlation_id=cid
            )
        return JSONResponse({"items": [item.model_dump(mode="json") for item in items]})

    @app.get("/api/v1/memory/export")
    async def export_memories(
        request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = _cid(request)
        items = await state.memory.export(correlation_id=cid)
        return JSONResponse(
            {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "exported_at": utcnow().isoformat(),
                "items": [item.model_dump(mode="json") for item in items],
            }
        )

    @app.get("/api/v1/memory/{item_id}")
    async def get_memory(
        item_id: str, request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.memory.get_authorized(item_id, correlation_id=cid)
        if item is None:
            raise _api_error(ErrorCode.NOT_FOUND, ErrorCategory.VALIDATION, "memory.not_found", cid)
        return JSONResponse(item.model_dump(mode="json"))

    @app.post("/api/v1/memory/{item_id}/supersede", status_code=201)
    async def supersede_memory(
        item_id: str,
        body: MemorySupersedeIn,
        request: Request,
        state: AppState = Depends(_get_state),
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.memory.supersede(
            item_id,
            title=body.title,
            content=body.content,
            confidence=body.confidence,
            sensitivity=Sensitivity(body.sensitivity) if body.sensitivity else None,
            correlation_id=cid,
        )
        return JSONResponse(item.model_dump(mode="json"), status_code=201)

    @app.post("/api/v1/memory/{item_id}/revoke")
    async def revoke_memory(
        item_id: str, request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.memory.revoke(item_id, correlation_id=cid)
        return JSONResponse(item.model_dump(mode="json"))

    @app.post("/api/v1/memory/{item_id}/confirm")
    async def confirm_memory(
        item_id: str, request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.memory.confirm(item_id, correlation_id=cid)
        return JSONResponse(item.model_dump(mode="json"))

    @app.delete("/api/v1/memory/{item_id}")
    async def delete_memory(
        item_id: str,
        request: Request,
        state: AppState = Depends(_get_state),
        purge: bool = Query(default=False),
    ) -> Response:
        cid = _cid(request)
        if purge:
            await state.memory.purge(item_id, correlation_id=cid)
            return Response(status_code=204)
        item = await state.memory.delete(item_id, correlation_id=cid)
        return JSONResponse(item.model_dump(mode="json"))

    # -- commitments API v1 (Phase 2, Slice 3) -------------------------------
    # Loopback-only. Mutations are explicit user commands: the service
    # enforces origin == "user_explicit_command" and a closed lifecycle
    # (open -> fulfilled / expired / cancelled; terminals immutable).
    # Audit summaries carry lengths/dates only — never titles or details.

    @app.get("/api/v1/commitments")
    async def list_commitments(
        request: Request,
        state: AppState = Depends(_get_state),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=20, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        cid = _cid(request)
        statuses = _commitment_status(status, cid)
        items = await state.commitments.list_commitments(
            statuses=statuses, limit=limit, offset=offset
        )
        return JSONResponse({"items": [item.model_dump(mode="json") for item in items]})

    @app.post("/api/v1/commitments", status_code=201)
    async def create_commitment(
        body: CommitmentCreateIn,
        request: Request,
        state: AppState = Depends(_get_state),
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.commitments.create(
            title=body.title,
            detail=body.detail,
            due_at=body.due_at,
            origin=USER_EXPLICIT_ORIGIN,
            correlation_id=cid,
        )
        return JSONResponse(item.model_dump(mode="json"), status_code=201)

    @app.post("/api/v1/commitments/{commitment_id}/fulfill")
    async def fulfill_commitment(
        commitment_id: str, request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.commitments.fulfill(
            commitment_id, origin=USER_EXPLICIT_ORIGIN, correlation_id=cid
        )
        return JSONResponse(item.model_dump(mode="json"))

    @app.post("/api/v1/commitments/{commitment_id}/cancel")
    async def cancel_commitment(
        commitment_id: str, request: Request, state: AppState = Depends(_get_state)
    ) -> JSONResponse:
        cid = _cid(request)
        item = await state.commitments.cancel(
            commitment_id, origin=USER_EXPLICIT_ORIGIN, correlation_id=cid
        )
        return JSONResponse(item.model_dump(mode="json"))

    # -- internal commitment sweep ------------------------------------------
    # Loopback-only. The idempotent expiry pass (§6.3); force=True bypasses
    # the 1h service_meta cooldown (the OS cron is the cadence). Documented
    # for a systemd timer / cron hook, e.g.:
    #   curl -X POST http://127.0.0.1:8123/api/v1/internal/commitments/sweep

    @app.post("/api/v1/internal/commitments/sweep")
    async def sweep_commitments(
        request: Request,
        state: AppState = Depends(_get_state),
        force: bool = Query(default=True),
    ) -> JSONResponse:
        cid = _cid(request)
        result = await state.commitments.sweep(force=force, correlation_id=cid)
        return JSONResponse(result.model_dump(mode="json"))

    return app
