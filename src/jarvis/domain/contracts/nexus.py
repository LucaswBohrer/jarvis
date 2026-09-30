"""NEXUS contracts.

Two layers:
  1. External DTOs (tolerant, extra=ignore) — valid ONLY at the adapter boundary.
  2. Canonical contracts (strict, frozen) — the only thing the core ever sees.

The adapter converts 1 -> 2 and never passes raw external JSON inward.
Field lists follow the NEXUS v1 API contract (equipment, summary, simulation).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .common import EvidenceRef, FrozenModel, TolerantModel, utcnow


class Availability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class DataState(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    EMPTY = "empty"
    INVALID = "invalid"


class ElectricalState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class EquipmentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


def _ci_enum(enum_cls: type[Enum], raw: str, field_name: str) -> Enum:
    """Case-insensitive enum parse. Unknown values fail — never approximated."""
    try:
        return enum_cls(str(raw).strip().lower())
    except ValueError:
        valid = sorted(m.value for m in enum_cls)
        raise ValueError(f"invalid {field_name} {raw!r}; expected one of {valid}") from None


class NexusStatusQuery(FrozenModel):
    equipment_code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # Discriminator for the ToolArguments union (memory.write/read args join it
    # in Phase 2). Default keeps every existing construction site valid.
    tool_arg_kind: Literal["nexus_status_query"] = "nexus_status_query"


# ---------------------------------------------------------------------------
# External DTOs — adapter boundary only (tolerant to additive fields).
# ---------------------------------------------------------------------------


class NexusEquipmentDTO(TolerantModel):
    id: str
    code: str
    name: str
    status: str
    enabled: bool

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value: object) -> object:
        # F3.6 real-contract fix: the NEXUS serves SQLite rowids, so `id`
        # arrives as int. Canonical contracts keep str ids; normalize at
        # the boundary so the core never sees the external shape.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


class NexusEquipmentListDTO(TolerantModel):
    equipment: list[NexusEquipmentDTO]


class NexusReadingDTO(TolerantModel):
    timestamp: AwareDatetime
    voltage: float
    current: float
    frequency: float
    power_factor: float
    active_power: float
    temperature: float
    status: str


class NexusAnomalyDTO(TolerantModel):
    code: str | None = None
    message: str
    severity: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_plain_code(cls, value: object) -> object:
        # F3.6 real-contract fix: the NEXUS diagnosis engine emits anomalies
        # as plain codes ("HIGH_VOLTAGE", "SENSOR_STALE"). A bare string
        # becomes the message; structured dicts keep working unchanged.
        if isinstance(value, str):
            return {"message": value}
        return value


class NexusRecommendationDTO(TolerantModel):
    id: str | None = None
    title: str | None = None
    message: str | None = None
    priority: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_plain_text(cls, value: object) -> object:
        # F3.6 real-contract fix: recommendations also arrive as plain
        # strings. Same normalization as anomalies.
        if isinstance(value, str):
            return {"message": value}
        return value


class NexusDiagnosisDTO(TolerantModel):
    status: str
    severity: str
    anomalies: list[NexusAnomalyDTO] = Field(default_factory=list)
    recommendations: list[NexusRecommendationDTO] = Field(default_factory=list)


class NexusSummaryDTO(TolerantModel):
    equipment: str
    last_reading: NexusReadingDTO | None = None
    last_reading_at: AwareDatetime | None = None
    diagnosis: NexusDiagnosisDTO | None = None
    active_events: int = 0
    open_episodes: int = 0
    readings_count: int = 0

    @field_validator("equipment", mode="before")
    @classmethod
    def _coerce_equipment(cls, value: object) -> object:
        # F3.6 real-contract fix: the NEXUS summary nests the full
        # equipment object. The adapter only needs its id for the
        # identity cross-check against the resolved equipment.
        if isinstance(value, dict):
            eid = value.get("id")
            if isinstance(eid, int) and not isinstance(eid, bool):
                return str(eid)
            return eid
        return value


class NexusSimulationDTO(TolerantModel):
    running: bool = False
    session_id: str | None = None
    mode: str | None = None
    intensity: float | None = None
    anomalies: list[str] = Field(default_factory=list)
    started_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    remaining_seconds: float | None = None


# ---------------------------------------------------------------------------
# Canonical contracts — strict, frozen, core-owned.
# ---------------------------------------------------------------------------


class EquipmentIdentity(FrozenModel):
    id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    status: EquipmentStatus
    enabled: bool


class Reading(FrozenModel):
    timestamp: AwareDatetime
    voltage_v: float
    current_a: float
    frequency_hz: float
    power_factor: float = Field(ge=-1.0, le=1.0)
    active_power_w: float
    temperature_c: float
    status: ElectricalState


class Anomaly(FrozenModel):
    code: str | None = Field(default=None, max_length=128)
    message: str = Field(min_length=1, max_length=1024)
    severity: Severity | None = None


class Recommendation(FrozenModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=1024)


class Diagnosis(FrozenModel):
    status: ElectricalState
    severity: Severity
    anomalies: list[Anomaly] = Field(default_factory=list, max_length=64)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=32)


class SimulationState(FrozenModel):
    running: bool = False
    mode: str | None = Field(default=None, max_length=64)
    started_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None


class CanonicalNexusStatus(FrozenModel):
    """Verified-shaped NEXUS snapshot. Built by the adapter, checked by verifiers."""

    equipment: EquipmentIdentity
    reading: Reading | None = None
    diagnosis: Diagnosis | None = None
    simulation: SimulationState = Field(default_factory=SimulationState)
    active_events: int = Field(ge=0)
    open_episodes: int = Field(ge=0)
    readings_count: int = Field(ge=0)
    observed_at: AwareDatetime = Field(default_factory=utcnow)


class CanonicalFact(FrozenModel):
    """One grounded fact. The LLM may select fact IDs; it may never create them."""

    id: str = Field(pattern=r"^FACT_[A-Z0-9_]{1,64}$")
    label: str = Field(min_length=1, max_length=128)
    value: str | int | float | bool | None = None
    unit: str | None = Field(default=None, max_length=32)


class VerifiedNexusStatus(FrozenModel):
    """Output of the verification pipeline. Only this enters LLM context."""

    availability: Availability
    data_state: DataState
    status: CanonicalNexusStatus | None = None
    facts: list[CanonicalFact] = Field(default_factory=list, max_length=128)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=32)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=8)
    verified_at: AwareDatetime = Field(default_factory=utcnow)

    @field_validator("facts")
    @classmethod
    def _unique_fact_ids(cls, facts: list[CanonicalFact]) -> list[CanonicalFact]:
        ids = [f.id for f in facts]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate fact ids")
        return facts


def parse_electrical_state(raw: str) -> ElectricalState:
    return _ci_enum(ElectricalState, raw, "electrical_state")  # type: ignore[return-value]


def parse_equipment_status(raw: str) -> EquipmentStatus:
    return _ci_enum(EquipmentStatus, raw, "equipment_status")  # type: ignore[return-value]


def parse_severity(raw: str) -> Severity:
    return _ci_enum(Severity, raw, "severity")  # type: ignore[return-value]
