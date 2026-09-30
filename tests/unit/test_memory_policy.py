"""T4 — Memory policy: exact-match gates on capability, method, actor, origin.

The critical case: a memory.write request whose arguments do NOT carry the
user_explicit_command origin must be DENIED — even though the contract
layer makes such a request unconstructible by normal means. The policy
rule is the second lock on the same door.
"""

from __future__ import annotations

from datetime import timedelta

from jarvis.domain.contracts.common import utcnow
from jarvis.domain.contracts.memory import (
    MemoryKind,
    MemoryQuery,
    MemoryWriteOp,
)
from jarvis.domain.contracts.policy import PolicyEffect, ReasonCode
from jarvis.domain.contracts.tool import (
    Capability,
    MemoryReadArgs,
    MemoryWriteArgs,
    NexusStatusQuery,
    ToolName,
    ToolRequest,
)


def _write_request(policy_args=None) -> ToolRequest:
    args = MemoryWriteArgs(
        op=MemoryWriteOp.CREATE,
        kind=MemoryKind.FACT,
        title="t",
        content="c",
        origin="user_explicit_command",
    )
    return ToolRequest(
        task_id="t1",
        correlation_id="c1",
        tool_name=ToolName.MEMORY_WRITE,
        capability=Capability.MEMORY_WRITE,
        arguments=args,
        deadline_at=utcnow() + timedelta(seconds=60),
    )


def _read_request() -> ToolRequest:
    return ToolRequest(
        task_id="t1",
        correlation_id="c1",
        tool_name=ToolName.MEMORY_READ,
        capability=Capability.MEMORY_READ,
        arguments=MemoryReadArgs(query=MemoryQuery(query="x")),
        deadline_at=utcnow() + timedelta(seconds=60),
    )


def _decide(
    policy, request, *, method="INTERNAL", url="internal://local-memory/", actor="local_user"
):
    return policy.decide(request, method=method, url=url, actor=actor)


def test_write_with_origin_allowed(policy):
    d = _decide(policy, _write_request())
    assert d.decision is PolicyEffect.ALLOW
    assert d.reason_code is ReasonCode.RULE_MATCH
    assert d.matched_rule_id == "memory-write-local"


def test_write_without_origin_denied(policy):
    # Bypass the contract pairing validator: the policy layer must still deny
    # a memory.write whose arguments carry no origin.
    forged = ToolRequest.model_construct(
        task_id="t1",
        correlation_id="c1",
        tool_name=ToolName.MEMORY_WRITE,
        capability=Capability.MEMORY_WRITE,
        arguments=NexusStatusQuery(equipment_code="DEFAULT"),  # no origin field
        deadline_at=utcnow() + timedelta(seconds=60),
    )
    d = _decide(policy, forged)
    assert d.decision is PolicyEffect.DENY
    assert d.reason_code is ReasonCode.CONSTRAINT_FAILED
    assert d.matched_rule_id == "memory-write-local"


def test_write_non_internal_method_denied(policy):
    d = _decide(policy, _write_request(), method="HTTP", url="http://127.0.0.1:8123/x")
    assert d.decision is PolicyEffect.DENY


def test_write_wrong_actor_denied(policy):
    d = _decide(policy, _write_request(), actor="someone_else")
    assert d.decision is PolicyEffect.DENY


def test_read_allowed(policy):
    d = _decide(policy, _read_request())
    assert d.decision is PolicyEffect.ALLOW
    assert d.matched_rule_id == "memory-read-local"


def test_read_non_internal_method_denied(policy):
    d = _decide(policy, _read_request(), method="GET", url="http://127.0.0.1:8123/api/v1/memory")
    assert d.decision is PolicyEffect.DENY


def test_unknown_capability_denied_by_default(policy):
    # A capability/tool pairing with no matching rule is denied.
    forged = ToolRequest.model_construct(
        task_id="t1",
        correlation_id="c1",
        tool_name=ToolName.MEMORY_READ,
        capability=Capability.NEXUS_STATUS_READ,  # pairing broken on purpose
        arguments=MemoryReadArgs(query=MemoryQuery(query="x")),
        deadline_at=utcnow() + timedelta(seconds=60),
    )
    d2 = policy.decide(forged, method="INTERNAL", url="internal://local-memory/")
    assert d2.decision is PolicyEffect.DENY
    assert d2.reason_code is ReasonCode.NO_MATCH


def test_policy_version_is_2(policy):
    # Version is namespaced by source file: "<stem>:<version>".
    assert policy.policy_version.endswith(":2")
