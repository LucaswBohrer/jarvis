"""Deterministic fake LLM provider. Used for tests and local dev.

Behaviors (set at construction):
  - VALID: builds a deterministic NexusResponsePlan from the request's facts.
  - TIMEOUT: sleeps past the caller's deadline (tests orchestrator enforcement).
  - CANCEL: sleeps until cancelled (CancelledError must propagate untouched).
  - MALFORMED: raises LLMMalformedError (simulates unparseable provider output).
  - UNKNOWN_FACT_ID: plan references a fact id absent from the snapshot.
  - UNKNOWN_RECOMMENDATION_ID: plan references an unknown recommendation id.

Every request is recorded so tests can assert the LLM only ever saw verified
facts. Usage/cost are deterministic; cost is None (no prices configured).
"""

from __future__ import annotations

import asyncio
from enum import Enum

from ...domain.contracts.llm import (
    DetailLevel,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    NexusResponsePlan,
    PlanTone,
    SummaryKey,
)
from ...ports.llm import LLMMalformedError
from ...verification.response import derive_summary_key, fact_str


class FakeBehavior(str, Enum):
    VALID = "valid"
    TIMEOUT = "timeout"
    CANCEL = "cancel"
    MALFORMED = "malformed"
    UNKNOWN_FACT_ID = "unknown_fact_id"
    UNKNOWN_RECOMMENDATION_ID = "unknown_recommendation_id"


def _fact_map(request: LLMRequest) -> dict[str, str]:
    return {f.id: fact_str(f.value) for f in request.facts}


def _derive_tone(key: SummaryKey) -> PlanTone:
    if key in (SummaryKey.CRITICAL, SummaryKey.ERROR):
        return PlanTone.URGENT
    if key in (
        SummaryKey.WARNING,
        SummaryKey.STALE,
        SummaryKey.EMPTY,
        SummaryKey.UNAVAILABLE,
        SummaryKey.SIMULATION,
    ):
        return PlanTone.ATTENTION
    return PlanTone.CALM


_CORE_FACT_IDS = (
    "FACT_NEXUS_AVAILABILITY",
    "FACT_DATA_STATE",
    "FACT_EQUIPMENT_NAME",
    "FACT_EQUIPMENT_CODE",
    "FACT_ELECTRICAL_STATUS",
    "FACT_DIAGNOSIS_SEVERITY",
    "FACT_SIMULATION_RUNNING",
    "FACT_LAST_READING_AT",
)


_MEASUREMENT_FACT_IDS = (
    "FACT_VOLTAGE_V",
    "FACT_CURRENT_A",
    "FACT_FREQUENCY_HZ",
    "FACT_POWER_FACTOR",
    "FACT_ACTIVE_POWER_W",
    "FACT_TEMPERATURE_C",
)


def _valid_plan(request: LLMRequest) -> NexusResponsePlan:
    facts = _fact_map(request)
    available = set(facts)
    selected = [fid for fid in _CORE_FACT_IDS if fid in available]
    # Deterministically add measurement facts when present.
    for fid in _MEASUREMENT_FACT_IDS:
        if fid in available and fid not in selected:
            selected.append(fid)
    # ... and every individual anomaly fact, so the renderer can list them.
    for fid in sorted(available):
        if fid.startswith("FACT_ANOMALY_") and fid != "FACT_ANOMALY_COUNT" and fid not in selected:
            selected.append(fid)
    if "FACT_ANOMALY_COUNT" in available and "FACT_ANOMALY_COUNT" not in selected:
        selected.append("FACT_ANOMALY_COUNT")
    key = derive_summary_key(facts)
    rec_ids = [r.id for r in request.recommendations]
    return NexusResponsePlan(
        summary_key=key,
        tone=_derive_tone(key),
        selected_fact_ids=selected,
        selected_recommendation_ids=rec_ids[:3],
        detail_level=DetailLevel.STANDARD,
    )


class FakeLLMProvider:
    """Deterministic stand-in. ``name`` is 'fake'."""

    name = "fake"

    def __init__(
        self,
        behavior: FakeBehavior = FakeBehavior.VALID,
        *,
        latency_ms: int = 5,
    ) -> None:
        self._behavior = behavior
        self._latency_ms = latency_ms
        self.requests: list[LLMRequest] = []

    @property
    def behavior(self) -> FakeBehavior:
        return self._behavior

    async def generate_structured(
        self, request: LLMRequest, *, timeout_seconds: float
    ) -> LLMResponse:
        self.requests.append(request)
        behavior = self._behavior
        if behavior is FakeBehavior.TIMEOUT:
            # Deliberately outlive the caller's deadline; orchestrator enforces.
            await asyncio.sleep(timeout_seconds + 30)
            raise AssertionError("unreachable: caller must enforce the timeout")
        if behavior is FakeBehavior.CANCEL:
            # Sleep until the caller cancels; CancelledError propagates untouched.
            await asyncio.sleep(3600)
            raise AssertionError("unreachable: caller must cancel")
        # Small deterministic latency for the fast paths.
        await asyncio.sleep(self._latency_ms / 1000)
        if behavior is FakeBehavior.MALFORMED:
            try:
                NexusResponsePlan.model_validate(
                    {
                        "summary_key": "not-a-key",
                        "selected_fact_ids": [],
                    }
                )
            except Exception as exc:
                raise LLMMalformedError(f"provider returned malformed plan: {exc}") from exc
            raise AssertionError("unreachable")
        plan = _valid_plan(request)
        if behavior is FakeBehavior.UNKNOWN_FACT_ID:
            plan = plan.model_copy(
                update={"selected_fact_ids": [*plan.selected_fact_ids, "FACT_NO_SUCH_FACT"]}
            )
        if behavior is FakeBehavior.UNKNOWN_RECOMMENDATION_ID:
            plan = plan.model_copy(update={"selected_recommendation_ids": ["rec-999"]})
        return LLMResponse(
            provider="fake",
            model="fake-deterministic-1",
            structured_output=plan,
            finish_reason="stop",
            latency_ms=self._latency_ms,
            usage=LLMUsage(
                input_tokens=sum(len(f.id) for f in request.facts),
                output_tokens=len(plan.selected_fact_ids) * 4,
                total_tokens=sum(len(f.id) for f in request.facts)
                + len(plan.selected_fact_ids) * 4,
                estimated_cost_usd=None,
            ),
        )

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
