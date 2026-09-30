"""Unit tests: policy decisions, redaction, and the task state machine."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from jarvis.domain.contracts.common import new_id, utcnow
from jarvis.domain.contracts.nexus import NexusStatusQuery
from jarvis.domain.contracts.policy import PolicyEffect, ReasonCode
from jarvis.domain.contracts.task import TaskInput, TaskKind, TaskRecord, TaskState
from jarvis.domain.contracts.tool import Capability, ToolName, ToolRequest
from jarvis.domain.errors import ErrorCode
from jarvis.domain.state_machine import (
    InvalidTransitionError,
    allowed_transitions,
    can_transition,
    transition,
)
from jarvis.security.policy import PolicyEngine
from jarvis.security.redaction import redact, redact_url

REPO = Path(__file__).resolve().parent.parent.parent


def _request(capability=Capability.NEXUS_STATUS_READ, tool=ToolName.NEXUS_STATUS):
    return ToolRequest(
        task_id=new_id(),
        correlation_id=new_id(),
        tool_name=tool,
        capability=capability,
        arguments=NexusStatusQuery(equipment_code="DEFAULT"),
        deadline_at=utcnow() + timedelta(seconds=5),
    )


@pytest.fixture()
def policy():
    return PolicyEngine.from_toml(str(REPO / "config" / "policy.toml"))


def test_policy_allows_configured_capability(policy):
    d = policy.decide(_request(), method="GET", url="http://127.0.0.1:8000/api/v1/equipment")
    assert d.decision == PolicyEffect.ALLOW
    assert d.reason_code == ReasonCode.RULE_MATCH


def test_policy_denies_unknown_capability(policy):
    d = policy.decide(
        _request(capability=Capability.NEXUS_STATUS_READ, tool=ToolName.NEXUS_STATUS),
        method="POST",
        url="http://127.0.0.1:8000/api/v1/equipment",
    )
    # POST is outside the rule's method constraint -> deny
    assert d.decision == PolicyEffect.DENY


def test_policy_deny_by_default_no_rule():
    engine = PolicyEngine(rules=[], policy_version="test")
    d = engine.decide(_request(), method="GET", url="http://127.0.0.1:8000/api/v1/equipment")
    assert d.decision == PolicyEffect.DENY
    assert d.reason_code == ReasonCode.NO_MATCH


def test_policy_rejects_non_loopback_destination(policy):
    d = policy.decide(_request(), method="GET", url="http://192.168.1.10/api/v1/equipment")
    assert d.decision == PolicyEffect.DENY


def test_policy_rejects_path_outside_prefix(policy):
    d = policy.decide(_request(), method="GET", url="http://127.0.0.1:8000/admin/secrets")
    assert d.decision == PolicyEffect.DENY


def test_redact_masks_known_secret_keys():
    payload = {
        "api_key": "sk-secret-123",
        "nested": {"password": "hunter2", "voltage": 220.0},
        "items": [{"token": "abc"}],
    }
    out = redact(payload)
    assert out["api_key"] == "[REDACTED]"
    assert out["nested"]["password"] == "[REDACTED]"  # noqa: S105
    assert out["nested"]["voltage"] == 220.0
    assert out["items"][0]["token"] == "[REDACTED]"  # noqa: S105
    # input not mutated
    assert payload["api_key"] == "sk-secret-123"


def test_redact_masks_authorization_header():
    headers = {"Authorization": "Bearer xyz", "Accept": "application/json"}
    out = redact(headers)
    assert out["Authorization"] == "[REDACTED]"
    assert out["Accept"] == "application/json"


def test_redact_url_strips_userinfo():
    url = "http://user:pass@127.0.0.1:8000/api/v1/x"
    out = redact_url(url)
    assert "user" not in out and "pass" not in out
    assert out == "http://127.0.0.1:8000/api/v1/x"


def _record(state=TaskState.PENDING, **over):
    base = {
        "id": new_id(),
        "session_id": new_id(),
        "kind": TaskKind.NEXUS_STATUS,
        "input": TaskInput(message_id=new_id(), utterance="como está o nexus?"),
        "state": state,
    }
    base.update(over)
    return TaskRecord(**base)


def test_state_machine_happy_path():
    r = _record(TaskState.PENDING)
    r = transition(r, TaskState.PLANNING)
    assert r.state == TaskState.PLANNING
    assert r.version == 2


def test_state_machine_terminal_immutable():
    r = _record(TaskState.FAILED, error_code=ErrorCode.POLICY_DENIED)
    assert r.state == TaskState.FAILED
    with pytest.raises(InvalidTransitionError):
        transition(r, TaskState.RUNNING)
    assert allowed_transitions(TaskState.FAILED) == frozenset()


def test_state_machine_rejects_skips():
    r = _record(TaskState.PENDING)
    assert not can_transition(TaskState.PENDING, TaskState.RUNNING)
    with pytest.raises(InvalidTransitionError):
        transition(r, TaskState.RUNNING)


def test_state_machine_guards():
    # PLANNING -> RUNNING requires a persisted plan
    r = _record(TaskState.PLANNING)
    with pytest.raises(InvalidTransitionError):
        transition(r, TaskState.RUNNING)
    # -> COMPLETED requires a result
    r2 = _record(TaskState.RUNNING)
    with pytest.raises(InvalidTransitionError):
        transition(r2, TaskState.COMPLETED)
    # -> FAILED requires an error_code
    with pytest.raises(InvalidTransitionError):
        transition(r2, TaskState.FAILED)


def test_state_machine_failed_carries_error_code():
    r = _record(TaskState.RUNNING)
    r = r.model_copy(update={"error_code": ErrorCode.POLICY_DENIED})
    r = transition(r, TaskState.FAILED)
    assert r.state == TaskState.FAILED
    assert r.error_code == ErrorCode.POLICY_DENIED
