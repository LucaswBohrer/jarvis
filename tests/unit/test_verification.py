"""Unit tests: NEXUS verification pipeline (transport -> schema -> semantic
-> freshness -> facts) and response grounding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.domain.contracts.llm import NexusResponsePlan, SummaryKey
from jarvis.domain.contracts.nexus import (
    Anomaly,
    Availability,
    CanonicalNexusStatus,
    DataState,
    Diagnosis,
    ElectricalState,
    EquipmentIdentity,
    EquipmentStatus,
    Reading,
    Severity,
    SimulationState,
)
from jarvis.domain.contracts.tool import ToolOutcome, ToolResult
from jarvis.domain.errors import ErrorCategory, ErrorCode, JarvisError
from jarvis.ports.clock import SystemClock
from jarvis.verification.nexus import verify_nexus_result
from jarvis.verification.response import (
    GroundingError,
    derive_summary_key,
    fact_str,
    verify_plan,
)


def _canonical(**over):
    now = datetime.now(UTC)
    base = {
        "equipment": EquipmentIdentity(
            id="eq-1",
            code="DEFAULT",
            name="Quadro Geral",
            status=EquipmentStatus.ACTIVE,
            enabled=True,
        ),
        "reading": Reading(
            timestamp=now,
            voltage_v=220.0,
            current_a=10.0,
            frequency_hz=60.0,
            power_factor=0.9,
            active_power_w=1980.0,
            temperature_c=40.0,
            status=ElectricalState.NORMAL,
        ),
        "diagnosis": Diagnosis(
            status=ElectricalState.NORMAL,
            severity=Severity.INFO,
            anomalies=[],
            recommendations=[],
        ),
        "simulation": SimulationState(running=False),
        "active_events": 0,
        "open_episodes": 0,
        "readings_count": 50,
        "observed_at": now,
    }
    base.update(over)
    return CanonicalNexusStatus(**base)


def _success_result(canonical, task_id="t1"):
    return ToolResult(
        request_id="r1",
        task_id=task_id,
        outcome=ToolOutcome.SUCCESS,
        data=canonical,
    )


def _failure_result(code, task_id="t1"):
    return ToolResult(
        request_id="r1",
        task_id=task_id,
        outcome=ToolOutcome.FAILURE,
        error=JarvisError(
            code=code,
            category=ErrorCategory.INTEGRATION,
            user_message_key="x",
            correlation_id="c",
        ),
    )


class FixedClock(SystemClock):
    def __init__(self, now):
        self._now = now

    def now(self):  # type: ignore[override]
        return self._now


def _fact_dict(verified):
    return {f.id: fact_str(f.value) for f in verified.facts}


def test_verify_fresh_success():
    now = datetime.now(UTC)
    v = verify_nexus_result(
        _success_result(_canonical()), clock=FixedClock(now), freshness_seconds=3600
    )
    assert v.availability == Availability.AVAILABLE
    assert v.data_state == DataState.FRESH
    facts = _fact_dict(v)
    assert facts["FACT_VOLTAGE_V"] == "220.0"
    assert facts["FACT_SIMULATION_RUNNING"] == "false"
    assert facts["FACT_NEXUS_AVAILABILITY"] == "available"


def test_verify_stale_when_reading_old():
    now = datetime.now(UTC)
    old = now - timedelta(seconds=600)
    canonical = _canonical(
        reading=Reading(
            timestamp=old,
            voltage_v=220.0,
            current_a=10.0,
            frequency_hz=60.0,
            power_factor=0.9,
            active_power_w=1980.0,
            temperature_c=40.0,
            status=ElectricalState.NORMAL,
        )
    )
    v = verify_nexus_result(_success_result(canonical), clock=FixedClock(now), freshness_seconds=60)
    assert v.data_state == DataState.STALE
    assert _fact_dict(v)["FACT_DATA_STATE"] == "stale"


def test_verify_empty_when_no_reading_and_no_diagnosis():
    v = verify_nexus_result(
        _success_result(_canonical(reading=None, diagnosis=None, readings_count=0)),
        clock=SystemClock(),
        freshness_seconds=60,
    )
    assert v.data_state == DataState.EMPTY


def test_verify_unavailable_on_tool_failure():
    v = verify_nexus_result(
        _failure_result(ErrorCode.NEXUS_UNAVAILABLE),
        clock=SystemClock(),
        freshness_seconds=60,
    )
    assert v.availability == Availability.UNAVAILABLE
    assert v.data_state == DataState.INVALID
    assert v.status is None
    assert _fact_dict(v)["FACT_NEXUS_AVAILABILITY"] == "unavailable"


def test_success_without_data_is_unrepresentable():
    """The ToolResult contract rejects SUCCESS with data=None at the boundary,
    so the verifier never has to handle that shape."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ToolResult(request_id="r1", task_id="t1", outcome=ToolOutcome.SUCCESS, data=None)


def test_facts_include_anomalies_individually():
    v = verify_nexus_result(
        _success_result(
            _canonical(
                diagnosis=Diagnosis(
                    status=ElectricalState.WARNING,
                    severity=Severity.MEDIUM,
                    anomalies=[
                        Anomaly(code="V-DROP", message="Queda de tensão", severity=Severity.MEDIUM)
                    ],
                    recommendations=[],
                )
            )
        ),
        clock=SystemClock(),
        freshness_seconds=3600,
    )
    facts = _fact_dict(v)
    assert facts["FACT_ANOMALY_COUNT"] == "1"
    assert facts["FACT_ANOMALY_0"] == "Queda de tensão"
    assert facts["FACT_DIAGNOSIS_SEVERITY"] == "medium"


def test_derive_summary_key_priority():
    base = {"FACT_NEXUS_AVAILABILITY": "available", "FACT_DATA_STATE": "fresh"}
    assert (
        derive_summary_key({**base, "FACT_NEXUS_AVAILABILITY": "unavailable"})
        == SummaryKey.UNAVAILABLE
    )
    assert derive_summary_key({**base, "FACT_DATA_STATE": "empty"}) == SummaryKey.EMPTY
    assert derive_summary_key({**base, "FACT_DATA_STATE": "stale"}) == SummaryKey.STALE
    assert derive_summary_key({**base, "FACT_SIMULATION_RUNNING": "true"}) == SummaryKey.SIMULATION
    assert derive_summary_key({**base, "FACT_ELECTRICAL_STATUS": "critical"}) == SummaryKey.CRITICAL
    assert derive_summary_key({**base, "FACT_ELECTRICAL_STATUS": "warning"}) == SummaryKey.WARNING
    assert derive_summary_key(base) == SummaryKey.NORMAL


def _verified():
    return verify_nexus_result(
        _success_result(_canonical()), clock=SystemClock(), freshness_seconds=3600
    )


def test_verify_plan_rejects_unknown_fact():
    with pytest.raises(GroundingError):
        verify_plan(
            NexusResponsePlan(
                summary_key=SummaryKey.NORMAL,
                selected_fact_ids=["FACT_NOPE"],
            ),
            _verified(),
        )


def test_verify_plan_rejects_contradictory_summary():
    with pytest.raises(GroundingError):
        verify_plan(
            NexusResponsePlan(
                summary_key=SummaryKey.CRITICAL,
                selected_fact_ids=["FACT_VOLTAGE_V"],
            ),
            _verified(),
        )


def test_verify_plan_accepts_valid_subset():
    plan = verify_plan(
        NexusResponsePlan(
            summary_key=SummaryKey.NORMAL,
            selected_fact_ids=["FACT_VOLTAGE_V", "FACT_NEXUS_AVAILABILITY"],
        ),
        _verified(),
    )
    assert plan.summary_key == SummaryKey.NORMAL


def test_fact_str_canonical_forms():
    assert fact_str(True) == "true"
    assert fact_str(False) == "false"
    assert fact_str(220.5) == "220.5"
    assert fact_str(None) == ""
