"""Read-only NEXUS HTTP adapter.

Knows nothing about sessions, tasks, LLM, or response templates. It translates
the three versioned NEXUS GETs into canonical JARVIS data and classifies every
failure as a typed ToolResult. Expected integration failures are returned, not
raised; CancelledError always propagates.

Hard boundaries (defense in depth, in addition to the policy gate):
  - GET only, paths built from constants under /api/v1/;
  - every URL re-validated as loopback with no userinfo before sending;
  - redirects never followed (3xx -> NEXUS_REDIRECT_BLOCKED);
  - response bodies capped at max_response_bytes;
  - total deadline + single retry for idempotent failures only;
  - in-memory circuit breaker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

import httpx

from ...domain.contracts.common import EvidenceRef, utcnow
from ...domain.contracts.nexus import (
    Anomaly,
    CanonicalNexusStatus,
    Diagnosis,
    EquipmentIdentity,
    NexusDiagnosisDTO,
    NexusEquipmentDTO,
    NexusEquipmentListDTO,
    NexusReadingDTO,
    NexusSimulationDTO,
    NexusStatusQuery,
    NexusSummaryDTO,
    Reading,
    Recommendation,
    SimulationState,
    parse_electrical_state,
    parse_equipment_status,
    parse_severity,
)
from ...domain.contracts.tool import (
    Capability,
    ToolName,
    ToolOutcome,
    ToolRequest,
    ToolResult,
)
from ...domain.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorSource,
    JarvisError,
    JarvisException,
)
from ...security.policy import validate_destination
from .circuit import CircuitBreaker

_PATH_EQUIPMENT = "/api/v1/equipment"
_PATH_PREFIX = "/api/v1/"


def _summary_path(equipment_id: str) -> str:
    return f"/api/v1/equipment/{quote(equipment_id, safe='')}/summary"


def _simulation_path(equipment_code: str) -> str:
    return f"/api/v1/simulation/status?equipment_id={quote(equipment_code, safe='')}"


@dataclass(frozen=True)
class NexusAdapterConfig:
    base_url: str
    connect_timeout_ms: int = 400
    read_timeout_ms: int = 1200
    pool_timeout_ms: int = 400
    total_deadline_ms: int = 3500
    max_retries: int = 1
    max_response_bytes: int = 262144
    circuit_failure_threshold: int = 3
    circuit_reset_seconds: float = 15.0


class _Failure(Exception):
    """Internal control flow for classified NEXUS failures."""

    def __init__(
        self,
        error: JarvisError,
        *,
        retry: bool,
        count_for_breaker: bool,
        evidence: list[EvidenceRef] | None = None,
    ) -> None:
        super().__init__(error.code.value)
        self.error = error
        self.retry = retry
        self.count_for_breaker = count_for_breaker
        self.evidence: list[EvidenceRef] = evidence or []


def _err(
    code: ErrorCode,
    category: ErrorCategory,
    key: str,
    correlation_id: str,
    *,
    retryable: bool = False,
    context: dict[str, str] | None = None,
) -> JarvisError:
    return JarvisError(
        code=code,
        category=category,
        user_message_key=key,
        retryable=retryable,
        source=ErrorSource.NEXUS,
        correlation_id=correlation_id,
        safe_context=context or {},
    )


class NexusHttpAdapter:
    """Implements the NexusIntegration port over httpx.AsyncClient."""

    def __init__(
        self,
        config: NexusAdapterConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        ok, reason = validate_destination(config.base_url)
        if not ok:
            raise ValueError(f"invalid NEXUS base_url: {reason}")
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._breaker = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            reset_seconds=config.circuit_reset_seconds,
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            # NEXUS is loopback-only by contract: never route through a proxy,
            # and never inherit proxy config from the environment.
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "jarvis-phase1/0.1.0"},
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Port entry point
    # ------------------------------------------------------------------

    async def get_status(self, request: ToolRequest) -> ToolResult:
        started_at = utcnow()
        evidence: list[EvidenceRef] = []
        attempts = {"n": 0}

        def _finish(
            outcome: ToolOutcome,
            *,
            data: CanonicalNexusStatus | None = None,
            error: JarvisError | None = None,
        ) -> ToolResult:
            finished = utcnow()
            return ToolResult(
                request_id=request.request_id,
                task_id=request.task_id,
                outcome=outcome,
                data=data,
                error=error,
                evidence=list(evidence),
                started_at=started_at,
                finished_at=finished,
                duration_ms=int((finished - started_at).total_seconds() * 1000),
                attempts=max(attempts["n"], 1),
            )

        # Fail closed: the adapter only serves its own capability/tool.
        if (
            request.tool_name is not ToolName.NEXUS_STATUS
            or request.capability is not Capability.NEXUS_STATUS_READ
        ):
            raise JarvisException(
                _err(
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "internal.error",
                    request.correlation_id,
                    context={"detail": "adapter received mismatched tool request"},
                )
            )
        # Fail closed: the adapter only understands NexusStatusQuery arguments.
        # A memory.write/read ToolRequest must never reach the NEXUS adapter.
        args = request.arguments
        if not isinstance(args, NexusStatusQuery):
            raise JarvisException(
                _err(
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "internal.error",
                    request.correlation_id,
                    context={"detail": "adapter received non-nexus arguments"},
                )
            )

        if not await self._breaker.can_execute():
            return _finish(
                ToolOutcome.FAILURE,
                error=_err(
                    ErrorCode.NEXUS_CIRCUIT_OPEN,
                    ErrorCategory.RESILIENCE,
                    "nexus.circuit_open",
                    request.correlation_id,
                    retryable=True,
                ),
            )

        cfg = self._config
        effective_deadline = min(
            request.deadline_at,
            started_at + timedelta(milliseconds=cfg.total_deadline_ms),
        )

        async def _counted_get(path: str) -> tuple[bytes, EvidenceRef]:
            return await self._get(
                path,
                request,
                deadline=effective_deadline,
                evidence=evidence,
                attempts=attempts,
            )

        try:
            body, _ = await _counted_get(_PATH_EQUIPMENT)
            equipment = self._resolve_equipment(
                body, equipment_code=args.equipment_code, correlation_id=request.correlation_id
            )
            t_summary = asyncio.create_task(_counted_get(_summary_path(equipment.id)))
            t_sim = asyncio.create_task(_counted_get(_simulation_path(args.equipment_code)))
            try:
                (summary_body, _), (sim_body, _) = await asyncio.gather(t_summary, t_sim)
            except _Failure:
                for pending in (t_summary, t_sim):
                    pending.cancel()
                await asyncio.gather(t_summary, t_sim, return_exceptions=True)
                raise
            canonical = self._to_canonical(
                equipment, summary_body, sim_body, request, observed_at=utcnow()
            )
            await self._breaker.record_success()
            return _finish(ToolOutcome.SUCCESS, data=canonical)
        except _Failure as failure:
            if failure.count_for_breaker:
                await self._breaker.record_failure()
            return _finish(ToolOutcome.FAILURE, error=failure.error)
        # CancelledError and unexpected bugs propagate: never swallowed.

    # ------------------------------------------------------------------
    # Single GET with retry, limits, and classification
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        request: ToolRequest,
        *,
        deadline: datetime,
        evidence: list[EvidenceRef],
        attempts: dict[str, int],
    ) -> tuple[bytes, EvidenceRef]:
        cfg = self._config
        url = self._base_url + path
        ok, reason = validate_destination(url)
        if not ok:
            raise _Failure(
                _err(
                    ErrorCode.NEXUS_INVALID_DESTINATION,
                    ErrorCategory.INTEGRATION,
                    "nexus.invalid_destination",
                    request.correlation_id,
                    context={"reason": reason},
                ),
                retry=False,
                count_for_breaker=False,
            )
        if not urlparse(url).path.startswith(_PATH_PREFIX):
            raise _Failure(
                _err(
                    ErrorCode.NEXUS_INVALID_DESTINATION,
                    ErrorCategory.INTEGRATION,
                    "nexus.invalid_destination",
                    request.correlation_id,
                ),
                retry=False,
                count_for_breaker=False,
            )

        backoff_base = 0.10
        attempt = 0
        while True:
            attempt += 1
            attempts["n"] += 1
            remaining = (deadline - utcnow()).total_seconds()
            if remaining <= 0.05:
                raise _Failure(
                    _err(
                        ErrorCode.NEXUS_TIMEOUT,
                        ErrorCategory.INTEGRATION,
                        "nexus.timeout",
                        request.correlation_id,
                        retryable=True,
                    ),
                    retry=False,
                    count_for_breaker=True,
                    evidence=list(evidence),
                )
            timeout = httpx.Timeout(
                connect=min(cfg.connect_timeout_ms / 1000, remaining),
                read=min(cfg.read_timeout_ms / 1000, remaining),
                write=min(cfg.read_timeout_ms / 1000, remaining),
                pool=min(cfg.pool_timeout_ms / 1000, remaining),
            )
            try:
                async with self._client.stream(
                    "GET", url, timeout=timeout, follow_redirects=False
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise _Failure(
                            _err(
                                ErrorCode.NEXUS_REDIRECT_BLOCKED,
                                ErrorCategory.INTEGRATION,
                                "nexus.redirect_blocked",
                                request.correlation_id,
                            ),
                            retry=False,
                            count_for_breaker=False,
                            evidence=list(evidence),
                        )
                    body = await self._read_limited(response, request, evidence)
                    ev = EvidenceRef(
                        method="GET",
                        path=urlparse(url).path
                        + (f"?{urlparse(url).query}" if urlparse(url).query else ""),
                        http_status=status,
                        body_sha256=hashlib.sha256(body).hexdigest(),
                    )
                    if status == 404:
                        raise _Failure(
                            _err(
                                ErrorCode.NEXUS_EQUIPMENT_NOT_FOUND,
                                ErrorCategory.INTEGRATION,
                                "nexus.not_found",
                                request.correlation_id,
                                context={"path": ev.path},
                            ),
                            retry=False,
                            count_for_breaker=False,
                            evidence=[*evidence, ev],
                        )
                    if status == 429:
                        raise _Failure(
                            _err(
                                ErrorCode.NEXUS_UNAVAILABLE,
                                ErrorCategory.INTEGRATION,
                                "nexus.unavailable",
                                request.correlation_id,
                                retryable=True,
                                context={"http_status": "429"},
                            ),
                            retry=False,
                            count_for_breaker=False,
                            evidence=[*evidence, ev],
                        )
                    if status in (502, 503, 504):
                        raise _Failure(
                            _err(
                                ErrorCode.NEXUS_UNAVAILABLE,
                                ErrorCategory.INTEGRATION,
                                "nexus.unavailable",
                                request.correlation_id,
                                retryable=True,
                                context={"http_status": str(status)},
                            ),
                            retry=True,
                            count_for_breaker=True,
                            evidence=[*evidence, ev],
                        )
                    if 400 <= status < 600:
                        raise _Failure(
                            _err(
                                ErrorCode.NEXUS_CONTRACT_INVALID,
                                ErrorCategory.INTEGRATION,
                                "nexus.contract_invalid",
                                request.correlation_id,
                                context={"http_status": str(status)},
                            ),
                            retry=False,
                            count_for_breaker=False,
                            evidence=[*evidence, ev],
                        )
                    evidence.append(ev)
                    return body, ev
            except _Failure as failure:
                if failure.retry and attempt <= cfg.max_retries:
                    remaining2 = (deadline - utcnow()).total_seconds()
                    delay = min(backoff_base + random.uniform(0, 0.1), max(remaining2 - 0.05, 0))  # noqa: S311 - backoff jitter, not cryptographic
                    if delay > 0:
                        await asyncio.sleep(delay)
                        continue
                    # No budget left: surface as timeout.
                    raise _Failure(
                        _err(
                            ErrorCode.NEXUS_TIMEOUT,
                            ErrorCategory.INTEGRATION,
                            "nexus.timeout",
                            request.correlation_id,
                            retryable=True,
                        ),
                        retry=False,
                        count_for_breaker=True,
                        evidence=failure.evidence,
                    ) from None
                raise
            except (httpx.TimeoutException, TimeoutError) as exc:
                op_failure = _Failure(
                    _err(
                        ErrorCode.NEXUS_TIMEOUT,
                        ErrorCategory.INTEGRATION,
                        "nexus.timeout",
                        request.correlation_id,
                        retryable=True,
                        context={"kind": type(exc).__name__},
                    ),
                    retry=True,
                    count_for_breaker=True,
                    evidence=list(evidence),
                )
                if attempt <= cfg.max_retries:
                    remaining2 = (deadline - utcnow()).total_seconds()
                    delay = min(backoff_base + random.uniform(0, 0.1), max(remaining2 - 0.05, 0))  # noqa: S311 - backoff jitter, not cryptographic
                    if delay > 0:
                        await asyncio.sleep(delay)
                        continue
                raise op_failure from exc
            except httpx.ConnectError as exc:
                op_failure = _Failure(
                    _err(
                        ErrorCode.NEXUS_UNAVAILABLE,
                        ErrorCategory.INTEGRATION,
                        "nexus.unavailable",
                        request.correlation_id,
                        retryable=True,
                        context={"kind": "connection_failed"},
                    ),
                    retry=True,
                    count_for_breaker=True,
                    evidence=list(evidence),
                )
                if attempt <= cfg.max_retries:
                    remaining2 = (deadline - utcnow()).total_seconds()
                    delay = min(backoff_base + random.uniform(0, 0.1), max(remaining2 - 0.05, 0))  # noqa: S311 - backoff jitter, not cryptographic
                    if delay > 0:
                        await asyncio.sleep(delay)
                        continue
                raise op_failure from exc
            except httpx.HTTPError as exc:
                raise _Failure(
                    _err(
                        ErrorCode.NEXUS_UNAVAILABLE,
                        ErrorCategory.INTEGRATION,
                        "nexus.unavailable",
                        request.correlation_id,
                        context={"kind": type(exc).__name__},
                    ),
                    retry=False,
                    count_for_breaker=True,
                    evidence=list(evidence),
                ) from exc
            # CancelledError is BaseException: never caught here, always propagates.

    async def _read_limited(
        self, response: httpx.Response, request: ToolRequest, evidence: list[EvidenceRef]
    ) -> bytes:
        limit = self._config.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise _Failure(
                    _err(
                        ErrorCode.NEXUS_RESPONSE_TOO_LARGE,
                        ErrorCategory.INTEGRATION,
                        "nexus.response_too_large",
                        request.correlation_id,
                        context={"limit_bytes": str(limit)},
                    ),
                    retry=False,
                    count_for_breaker=False,
                    evidence=list(evidence),
                )
            chunks.append(chunk)
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # DTO -> canonical conversion (adapter boundary)
    # ------------------------------------------------------------------

    def _resolve_equipment(
        self, body: bytes, *, equipment_code: str, correlation_id: str
    ) -> NexusEquipmentDTO:
        try:
            payload = json.loads(body)
            dto = NexusEquipmentListDTO.model_validate(payload)
        except Exception as exc:
            raise _Failure(
                _err(
                    ErrorCode.NEXUS_CONTRACT_INVALID,
                    ErrorCategory.INTEGRATION,
                    "nexus.contract_invalid",
                    correlation_id,
                    context={"endpoint": _PATH_EQUIPMENT},
                ),
                retry=False,
                count_for_breaker=False,
            ) from exc
        match = next((e for e in dto.equipment if e.code == equipment_code), None)
        if match is None:
            raise _Failure(
                _err(
                    ErrorCode.NEXUS_EQUIPMENT_NOT_FOUND,
                    ErrorCategory.INTEGRATION,
                    "nexus.not_found",
                    correlation_id,
                    context={"equipment_code": equipment_code},
                ),
                retry=False,
                count_for_breaker=False,
            )
        return match

    def _to_canonical(
        self,
        equipment_dto: NexusEquipmentDTO,
        summary_body: bytes,
        sim_body: bytes,
        request: ToolRequest,
        *,
        observed_at: datetime,
    ) -> CanonicalNexusStatus:
        try:
            summary = NexusSummaryDTO.model_validate(json.loads(summary_body))
            simulation = NexusSimulationDTO.model_validate(json.loads(sim_body))
        except Exception as exc:
            raise _Failure(
                _err(
                    ErrorCode.NEXUS_CONTRACT_INVALID,
                    ErrorCategory.INTEGRATION,
                    "nexus.contract_invalid",
                    request.correlation_id,
                ),
                retry=False,
                count_for_breaker=False,
            ) from exc
        try:
            if summary.equipment != equipment_dto.id:
                raise ValueError(
                    f"summary equipment {summary.equipment!r} != resolved id {equipment_dto.id!r}"
                )
            equipment = EquipmentIdentity(
                id=equipment_dto.id,
                code=equipment_dto.code,
                name=equipment_dto.name,
                status=parse_equipment_status(equipment_dto.status),
                enabled=equipment_dto.enabled,
            )
            reading = self._to_reading(summary.last_reading) if summary.last_reading else None
            diagnosis = self._to_diagnosis(summary.diagnosis) if summary.diagnosis else None
            for count_name, count in (
                ("active_events", summary.active_events),
                ("open_episodes", summary.open_episodes),
                ("readings_count", summary.readings_count),
            ):
                if count < 0:
                    raise ValueError(f"{count_name} is negative")
            return CanonicalNexusStatus(
                equipment=equipment,
                reading=reading,
                diagnosis=diagnosis,
                simulation=SimulationState(
                    running=simulation.running,
                    mode=simulation.mode,
                    started_at=simulation.started_at,
                    ends_at=simulation.ends_at,
                ),
                active_events=summary.active_events,
                open_episodes=summary.open_episodes,
                readings_count=summary.readings_count,
                observed_at=observed_at,
            )
        except _Failure:
            raise
        except Exception as exc:
            raise _Failure(
                _err(
                    ErrorCode.NEXUS_CONTRACT_INVALID,
                    ErrorCategory.INTEGRATION,
                    "nexus.contract_invalid",
                    request.correlation_id,
                    context={"detail": str(exc)[:120]},
                ),
                retry=False,
                count_for_breaker=False,
            ) from exc

    @staticmethod
    def _to_reading(dto: NexusReadingDTO) -> Reading:
        return Reading(
            timestamp=dto.timestamp,
            voltage_v=dto.voltage,
            current_a=dto.current,
            frequency_hz=dto.frequency,
            power_factor=dto.power_factor,
            active_power_w=dto.active_power,
            temperature_c=dto.temperature,
            status=parse_electrical_state(dto.status),
        )

    @staticmethod
    def _to_diagnosis(dto: NexusDiagnosisDTO) -> Diagnosis:
        anomalies = [
            Anomaly(
                code=a.code,
                message=a.message,
                severity=parse_severity(a.severity) if a.severity else None,
            )
            for a in dto.anomalies
        ]
        recommendations: list[Recommendation] = []
        for i, r in enumerate(dto.recommendations):
            text = r.title or r.message
            if not text:
                raise ValueError(f"recommendation {i} has no text")
            recommendations.append(Recommendation(id=r.id or f"rec-{i}", text=text))
        return Diagnosis(
            status=parse_electrical_state(dto.status),
            severity=parse_severity(dto.severity),
            anomalies=anomalies,
            recommendations=recommendations,
        )
