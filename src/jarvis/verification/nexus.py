"""NEXUS verification pipeline: transport -> schema -> semantic -> freshness -> facts.

Input: typed ToolResult from the adapter.
Output: VerifiedNexusStatus — the ONLY thing allowed into LLM context.

Classification (no inference, no approximation):
  - tool FAILURE            -> AVAILABLE=unavailable, DATA_STATE=invalid, status=None
  - reading present+fresh   -> available / fresh
  - reading present+old     -> available / stale
  - no reading, count == 0  -> available / empty
  - anything inconsistent   -> unavailable / invalid (never presented as data)
"""

from __future__ import annotations

from ..domain.contracts.nexus import (
    Availability,
    CanonicalFact,
    CanonicalNexusStatus,
    DataState,
    Recommendation,
    VerifiedNexusStatus,
)
from ..domain.contracts.tool import ToolOutcome, ToolResult
from ..ports.clock import Clock


def _fact(
    fact_id: str, label: str, value: str | int | float | bool | None, unit: str | None = None
) -> CanonicalFact:
    return CanonicalFact(id=fact_id, label=label, value=value, unit=unit)


def build_facts(status: CanonicalNexusStatus, data_state: DataState) -> list[CanonicalFact]:
    facts: list[CanonicalFact] = [
        _fact("FACT_NEXUS_AVAILABILITY", "NEXUS reachability", Availability.AVAILABLE.value),
        _fact("FACT_DATA_STATE", "Data freshness classification", data_state.value),
        _fact("FACT_EQUIPMENT_NAME", "Equipment name", status.equipment.name),
        _fact("FACT_EQUIPMENT_CODE", "Equipment code", status.equipment.code),
        _fact("FACT_EQUIPMENT_STATUS", "Equipment status", status.equipment.status.value),
    ]
    electrical = None
    if status.diagnosis is not None:
        electrical = status.diagnosis.status.value
        facts.append(_fact("FACT_ELECTRICAL_STATUS", "Electrical status", electrical))
        facts.append(
            _fact("FACT_DIAGNOSIS_SEVERITY", "Diagnosis severity", status.diagnosis.severity.value)
        )
        facts.append(_fact("FACT_ANOMALY_COUNT", "Anomaly count", len(status.diagnosis.anomalies)))
        for i, anomaly in enumerate(status.diagnosis.anomalies):
            facts.append(_fact(f"FACT_ANOMALY_{i}", f"Anomaly {i + 1}", anomaly.message))
    elif status.reading is not None:
        electrical = status.reading.status.value
        facts.append(_fact("FACT_ELECTRICAL_STATUS", "Electrical status", electrical))
    if status.reading is not None:
        reading = status.reading
        facts.append(
            _fact("FACT_LAST_READING_AT", "Last reading timestamp", reading.timestamp.isoformat())
        )
        facts.append(_fact("FACT_VOLTAGE_V", "Voltage", reading.voltage_v, "V"))
        facts.append(_fact("FACT_CURRENT_A", "Current", reading.current_a, "A"))
        facts.append(_fact("FACT_FREQUENCY_HZ", "Frequency", reading.frequency_hz, "Hz"))
        facts.append(_fact("FACT_POWER_FACTOR", "Power factor", reading.power_factor))
        facts.append(_fact("FACT_ACTIVE_POWER_W", "Active power", reading.active_power_w, "W"))
        facts.append(_fact("FACT_TEMPERATURE_C", "Temperature", reading.temperature_c, "°C"))
    facts.append(_fact("FACT_ACTIVE_EVENTS", "Active events", status.active_events))
    facts.append(_fact("FACT_OPEN_EPISODES", "Open episodes", status.open_episodes))
    facts.append(_fact("FACT_READINGS_COUNT", "Readings count", status.readings_count))
    facts.append(_fact("FACT_SIMULATION_RUNNING", "Simulation running", status.simulation.running))
    if status.simulation.running and status.simulation.mode:
        facts.append(_fact("FACT_SIMULATION_MODE", "Simulation mode", status.simulation.mode))
    return facts


def _recommendations(status: CanonicalNexusStatus) -> list[Recommendation]:
    if status.diagnosis is None:
        return []
    return list(status.diagnosis.recommendations)


def verify_nexus_result(
    result: ToolResult, *, clock: Clock, freshness_seconds: int
) -> VerifiedNexusStatus:
    """Run the full verification pipeline over an adapter ToolResult."""
    now = clock.now()

    # 1. Transport: a failed tool call is not data.
    if result.outcome is not ToolOutcome.SUCCESS or result.data is None:
        return VerifiedNexusStatus(
            availability=Availability.UNAVAILABLE,
            data_state=DataState.INVALID,
            status=None,
            facts=[
                _fact(
                    "FACT_NEXUS_AVAILABILITY", "NEXUS reachability", Availability.UNAVAILABLE.value
                ),
                _fact("FACT_DATA_STATE", "Data freshness classification", DataState.INVALID.value),
            ],
            recommendations=[],
            evidence=list(result.evidence),
            verified_at=now,
        )

    status_data = result.data
    if not isinstance(status_data, CanonicalNexusStatus):
        # The verifier only understands NEXUS payloads. Anything else is a
        # bug upstream; unverified data must never become facts.
        return VerifiedNexusStatus(
            availability=Availability.UNAVAILABLE,
            data_state=DataState.INVALID,
            status=None,
            facts=[
                _fact(
                    "FACT_NEXUS_AVAILABILITY", "NEXUS reachability", Availability.UNAVAILABLE.value
                ),
                _fact("FACT_DATA_STATE", "Data freshness classification", DataState.INVALID.value),
            ],
            recommendations=[],
            evidence=list(result.evidence),
            verified_at=now,
        )
    status: CanonicalNexusStatus = status_data

    # 2-3. Schema (defense in depth) + semantic consistency.
    data_state = _classify(status, clock=clock, freshness_seconds=freshness_seconds)
    if data_state is DataState.INVALID:
        return VerifiedNexusStatus(
            availability=Availability.UNAVAILABLE,
            data_state=DataState.INVALID,
            status=None,
            facts=[
                _fact(
                    "FACT_NEXUS_AVAILABILITY", "NEXUS reachability", Availability.UNAVAILABLE.value
                ),
                _fact("FACT_DATA_STATE", "Data freshness classification", DataState.INVALID.value),
            ],
            recommendations=[],
            evidence=list(result.evidence),
            verified_at=now,
        )

    return VerifiedNexusStatus(
        availability=Availability.AVAILABLE,
        data_state=data_state,
        status=status,
        facts=build_facts(status, data_state),
        recommendations=_recommendations(status),
        evidence=list(result.evidence),
        verified_at=now,
    )


def _classify(status: CanonicalNexusStatus, *, clock: Clock, freshness_seconds: int) -> DataState:
    # Semantic consistency: reading and diagnosis come together or not at all.
    if (status.reading is None) != (status.diagnosis is None):
        return DataState.INVALID
    if status.reading is None:
        # EMPTY only when the NEXUS genuinely has no readings.
        return DataState.EMPTY if status.readings_count == 0 else DataState.INVALID
    # Freshness against the injectable clock. Future timestamps (clock skew)
    # are treated as fresh rather than punished.
    age_seconds = (clock.now() - status.reading.timestamp).total_seconds()
    if age_seconds <= freshness_seconds:
        return DataState.FRESH
    return DataState.STALE


# Re-export for the orchestrator/tests.
__all__ = ["build_facts", "verify_nexus_result"]
