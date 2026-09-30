"""T1 — Memory contracts: schema, discriminated union, origin enforcement.

The contract layer is the first line of defense: a memory write without an
explicit-user-command origin must be a *contract violation* (not a policy
decision), and the tool/arguments/capability pairing must be airtight.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from jarvis.domain.contracts.common import utcnow
from jarvis.domain.contracts.memory import (
    MEMORY_SCHEMA_VERSION,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryStatus,
    MemoryWriteOp,
    Provenance,
    Sensitivity,
)
from jarvis.domain.contracts.tool import (
    Capability,
    MemoryReadArgs,
    MemoryWriteArgs,
    NexusStatusQuery,
    ToolName,
    ToolRequest,
)


def _write_args(**over):
    base = {
        "op": MemoryWriteOp.CREATE,
        "kind": MemoryKind.FACT,
        "title": "t",
        "content": "c",
        "origin": "user_explicit_command",
    }
    base.update(over)
    return MemoryWriteArgs(**base)


def test_schema_version_is_2():
    assert MEMORY_SCHEMA_VERSION == 2


def test_origin_is_required_singleton():
    # No default: omitting origin is a contract violation.
    with pytest.raises(ValidationError):
        MemoryWriteArgs(op=MemoryWriteOp.CREATE, kind=MemoryKind.FACT, title="t", content="c")


def test_origin_rejects_anything_else():
    with pytest.raises(ValidationError):
        _write_args(origin="llm_inference")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _write_args(origin="user")  # type: ignore[arg-type]


def test_create_requires_kind_title_content():
    with pytest.raises(ValidationError):
        _write_args(kind=None)
    with pytest.raises(ValidationError):
        _write_args(title="")
    with pytest.raises(ValidationError):
        _write_args(content=None)
    with pytest.raises(ValidationError):
        _write_args(target_id="x")  # create must not carry target_id


def test_supersede_requires_target_and_new_data():
    args = MemoryWriteArgs(
        op=MemoryWriteOp.SUPERSEDE,
        target_id="abc",
        title="new title",
        origin="user_explicit_command",
    )
    assert args.op is MemoryWriteOp.SUPERSEDE
    with pytest.raises(ValidationError):
        MemoryWriteArgs(op=MemoryWriteOp.SUPERSEDE, title="t", origin="user_explicit_command")
    with pytest.raises(ValidationError):
        MemoryWriteArgs(
            op=MemoryWriteOp.SUPERSEDE,
            target_id="abc",
            origin="user_explicit_command",
        )
    with pytest.raises(ValidationError):
        MemoryWriteArgs(
            op=MemoryWriteOp.SUPERSEDE,
            target_id="abc",
            kind=MemoryKind.FACT,  # kind is inherited, not set
            title="t",
            origin="user_explicit_command",
        )


def test_other_ops_take_only_target_id():
    for op in (
        MemoryWriteOp.REVOKE,
        MemoryWriteOp.DELETE,
        MemoryWriteOp.CONFIRM,
        MemoryWriteOp.PURGE,
    ):
        args = MemoryWriteArgs(op=op, target_id="abc", origin="user_explicit_command")
        assert args.op is op
        with pytest.raises(ValidationError):
            MemoryWriteArgs(op=op, origin="user_explicit_command")  # no target
        with pytest.raises(ValidationError):
            MemoryWriteArgs(
                op=op,
                target_id="abc",
                title="t",  # payload not allowed
                origin="user_explicit_command",
            )


def test_tool_request_pairs_tool_arguments_capability():
    args = _write_args()
    req = ToolRequest(
        task_id="t1",
        correlation_id="c1",
        tool_name=ToolName.MEMORY_WRITE,
        capability=Capability.MEMORY_WRITE,
        arguments=args,
        deadline_at=utcnow() + timedelta(seconds=60),
    )
    assert req.arguments.tool_arg_kind == "memory_write"
    # Mismatched capability is rejected at the contract level.
    with pytest.raises(ValidationError):
        ToolRequest(
            task_id="t1",
            correlation_id="c1",
            tool_name=ToolName.MEMORY_WRITE,
            capability=Capability.NEXUS_STATUS_READ,
            arguments=args,
            deadline_at=utcnow() + timedelta(seconds=60),
        )
    # MemoryWriteArgs cannot ride on the NEXUS tool name either.
    with pytest.raises(ValidationError):
        ToolRequest(
            task_id="t1",
            correlation_id="c1",
            tool_name=ToolName.NEXUS_STATUS,
            capability=Capability.MEMORY_WRITE,
            arguments=args,
            deadline_at=utcnow() + timedelta(seconds=60),
        )


def test_discriminated_union_dispatches_by_kind():
    args = _write_args()
    parsed = MemoryWriteArgs.model_validate(args.model_dump(mode="json"))
    assert isinstance(parsed, MemoryWriteArgs)
    read = MemoryReadArgs(query=MemoryQuery(query="hello"))
    parsed_read = MemoryReadArgs.model_validate(read.model_dump(mode="json"))
    assert isinstance(parsed_read, MemoryReadArgs)


def test_memory_query_validation():
    q = MemoryQuery(query="café com leite", limit=5)
    assert q.limit == 5
    assert q.schema_version == MEMORY_SCHEMA_VERSION
    with pytest.raises(ValidationError):
        MemoryQuery(query="")
    with pytest.raises(ValidationError):
        MemoryQuery(query="x", limit=0)
    with pytest.raises(ValidationError):
        MemoryQuery(query="x", limit=51)
    # include_pending exists and defaults to False (guarantee of the slice)
    assert MemoryQuery(query="x").include_pending is False


def test_memory_item_defaults():
    item = MemoryItem(
        kind=MemoryKind.PREFERENCE,
        title="t",
        content="c",
        provenance=Provenance.USER_EXPLICIT,
        confidence=1.0,
    )
    assert item.provenance is Provenance.USER_EXPLICIT
    assert item.status is MemoryStatus.PENDING
    assert item.sensitivity is Sensitivity.STANDARD
    assert item.schema_version == MEMORY_SCHEMA_VERSION


def test_terminal_statuses_are_terminal():
    from jarvis.domain.contracts.memory import TERMINAL_STATUSES

    assert MemoryStatus.ACTIVE not in TERMINAL_STATUSES
    assert MemoryStatus.PENDING not in TERMINAL_STATUSES
    for s in (
        MemoryStatus.SUPERSEDED,
        MemoryStatus.EXPIRED,
        MemoryStatus.REVOKED,
        MemoryStatus.DELETED,
    ):
        assert s in TERMINAL_STATUSES


def test_nexus_status_query_has_discriminator():
    q = NexusStatusQuery(equipment_code="DEFAULT")
    assert q.tool_arg_kind == "nexus_status_query"
