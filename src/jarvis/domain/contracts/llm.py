"""LLM contracts. The provider returns a response *plan*, never factual prose."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator

from .common import SCHEMA_VERSION, FrozenModel, new_id
from .context import ContextSection
from .nexus import CanonicalFact, Recommendation


class LLMPurpose(str, Enum):
    NEXUS_RESPONSE_PLAN = "nexus_response_plan"


class SummaryKey(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    STALE = "stale"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    SIMULATION = "simulation"


class PlanTone(str, Enum):
    CALM = "calm"
    ATTENTION = "attention"
    URGENT = "urgent"


class DetailLevel(str, Enum):
    BRIEF = "brief"
    STANDARD = "standard"


class LLMUsage(FrozenModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)


class NexusResponsePlan(FrozenModel):
    """Structured presentation plan. References facts by ID; creates none."""

    summary_key: SummaryKey
    tone: PlanTone = PlanTone.CALM
    selected_fact_ids: list[str] = Field(min_length=1, max_length=64)
    selected_recommendation_ids: list[str] = Field(default_factory=list, max_length=32)
    detail_level: DetailLevel = DetailLevel.STANDARD

    @field_validator("selected_fact_ids")
    @classmethod
    def _fact_id_shape(cls, ids: list[str]) -> list[str]:
        import re

        pattern = re.compile(r"^FACT_[A-Z0-9_]{1,64}$")
        for fid in ids:
            if not pattern.match(fid):
                raise ValueError(f"malformed fact id {fid!r}")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate fact ids")
        return ids


class LLMRequest(FrozenModel):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    request_id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    purpose: LLMPurpose = LLMPurpose.NEXUS_RESPONSE_PLAN
    locale: str = Field(default="pt-BR", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    facts: list[CanonicalFact] = Field(min_length=1, max_length=128)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=32)
    #: Labeled context sections (conversation tail, preferences, memories).
    #: Added in Phase 2 Slice 2 (§12): sensitive memories are excluded here
    #: by the ContextBuilder — this object never carries them to a provider.
    context_sections: tuple[ContextSection, ...] = Field(default=())
    max_output_tokens: int = Field(default=300, ge=16, le=4096)
    metadata: dict[str, str] = Field(default_factory=dict)


class LLMResponse(FrozenModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    structured_output: NexusResponsePlan
    finish_reason: str = Field(min_length=1, max_length=64)
    latency_ms: int = Field(ge=0)
    usage: LLMUsage
