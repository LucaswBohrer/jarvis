"""Policy contracts: deterministic decisions, executable rules."""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field, field_validator

from .common import FrozenModel, utcnow


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class ReasonCode(str, Enum):
    RULE_MATCH = "rule_match"
    NO_MATCH = "no_match"
    CONSTRAINT_FAILED = "constraint_failed"
    INVALID_DESTINATION = "invalid_destination"


class PolicyConstraints(FrozenModel):
    methods: list[str] = Field(default_factory=list, max_length=8)
    path_prefix: str = Field(default="", max_length=128)
    loopback_only: bool = False
    follow_redirects: bool = False
    max_response_bytes: int = Field(default=0, ge=0)
    deadline_ms: int = Field(default=0, ge=0)


class PermissionRule(FrozenModel):
    """One static rule. No wildcards in Phase 1 — capability/tool/resource are
    exact strings; the loader rejects '*' anywhere."""

    id: str = Field(min_length=1, max_length=64)
    effect: PolicyEffect
    capability: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=128)
    methods: list[str] = Field(min_length=1, max_length=8)
    path_prefix: str = Field(min_length=1, max_length=128)
    loopback_only: bool = True
    follow_redirects: bool = False
    max_response_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    deadline_ms: int = Field(gt=0, le=120_000)
    # Phase 2: internal tools (memory.write/read) constrain WHO may invoke and
    # WHICH argument origin is accepted. Both are optional exact matches —
    # never wildcards. A request whose actor or origin does not match is denied.
    actor: str | None = Field(default=None, min_length=1, max_length=64)
    require_origin: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("capability", "tool", "resource", "id")
    @classmethod
    def _no_wildcards(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("wildcards are forbidden in Phase 1 policy")
        return value

    @field_validator("methods")
    @classmethod
    def _methods_upper(cls, value: list[str]) -> list[str]:
        methods = [m.upper() for m in value]
        if any("*" in m for m in methods):
            raise ValueError("wildcards are forbidden in Phase 1 policy")
        return methods


class PolicyDecision(FrozenModel):
    decision: PolicyEffect
    reason_code: ReasonCode
    policy_version: str = Field(min_length=1, max_length=64)
    matched_rule_id: str | None = Field(default=None, max_length=64)
    constraints: PolicyConstraints = Field(default_factory=PolicyConstraints)
    evaluated_at: AwareDatetime = Field(default_factory=utcnow)
