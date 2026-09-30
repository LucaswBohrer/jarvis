"""Deterministic policy engine. Policy is executable code + policy.toml.

It is NEVER a prompt. No user text, NEXUS content, tool output, or LLM output
can create or widen a rule. Default is DENY. Precedence: DENY > CONFIRM > ALLOW.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

from ..domain.contracts.common import utcnow
from ..domain.contracts.policy import (
    PermissionRule,
    PolicyConstraints,
    PolicyDecision,
    PolicyEffect,
    ReasonCode,
)
from ..domain.contracts.tool import Capability, MemoryWriteArgs, ToolName, ToolRequest


def _request_origin(request: ToolRequest) -> str | None:
    """Return the provenance origin of a memory.write request's arguments.

    Only MemoryWriteArgs carries an origin (a singleton literal); every other
    arguments type returns None, which fails any require_origin rule.
    """
    arguments = request.arguments
    if isinstance(arguments, MemoryWriteArgs):
        return arguments.origin
    return None


def _is_loopback_host(host: str) -> bool:
    """True only if the host is literally loopback.

    Literal IPs are checked without DNS. Hostnames are resolved and *every*
    resolved address must be loopback — a single non-loopback address fails.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass  # not a literal IP -> resolve
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_loopback for info in infos)


def validate_destination(url: str) -> tuple[bool, str]:
    """Check a URL is a sane loopback HTTP(S) destination.

    Returns (ok, reason). Rejects userinfo, fragments, non-http schemes,
    and anything that does not resolve exclusively to loopback.
    """
    try:
        parts = urlparse(url)
    except ValueError as exc:
        return False, f"unparseable url: {exc}"
    if parts.scheme not in ("http", "https"):
        return False, f"scheme {parts.scheme!r} not allowed"
    if parts.username or parts.password:
        return False, "userinfo in url is forbidden"
    if parts.fragment:
        return False, "fragment in url is forbidden"
    if not parts.hostname:
        return False, "missing hostname"
    if not _is_loopback_host(parts.hostname):
        return False, f"non-loopback destination {parts.hostname!r}"
    return True, "ok"


class PolicyEngine:
    """Loads static rules from TOML and evaluates ToolRequests deterministically."""

    def __init__(self, rules: list[PermissionRule], policy_version: str) -> None:
        self._rules = list(rules)
        self._version = policy_version

    @property
    def policy_version(self) -> str:
        return self._version

    @property
    def rules(self) -> list[PermissionRule]:
        return list(self._rules)

    @classmethod
    def from_toml(cls, path: str | Path) -> PolicyEngine:
        import tomllib

        path = Path(path)
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        version = str(data.get("policy_version", ""))
        if not version:
            raise ValueError("policy.toml: missing policy_version")
        raw_rules = data.get("rule", [])
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("policy.toml: no rules defined (deny-by-default needs explicit rules)")
        rules = [PermissionRule.model_validate(r) for r in raw_rules]
        # PermissionRule validators already reject wildcards; double-check ids unique.
        ids = [r.id for r in rules]
        if len(set(ids)) != len(ids):
            raise ValueError("policy.toml: duplicate rule ids")
        return cls(rules, policy_version=f"{path.name}:{version}")

    def decide(
        self, request: ToolRequest, *, method: str, url: str, actor: str = "local_user"
    ) -> PolicyDecision:
        """Decide whether this exact (capability, tool, method, url) may execute.

        The actor defaults to the local single user (Phase 1/2a). Rules may
        additionally constrain the actor and the tool-arguments origin; both
        are exact matches, and a mismatch denies the request.
        """
        now = utcnow()
        capability = request.capability.value
        tool_name = request.tool_name.value
        matches = [r for r in self._rules if r.capability == capability and r.tool == tool_name]
        if not matches:
            return PolicyDecision(
                decision=PolicyEffect.DENY,
                reason_code=ReasonCode.NO_MATCH,
                policy_version=self._version,
                matched_rule_id=None,
                evaluated_at=now,
            )
        method = method.upper()
        request_origin = _request_origin(request)
        # Precedence: an explicit DENY on a matching rule wins over everything.
        ordered = sorted(
            matches,
            key=lambda r: {PolicyEffect.DENY: 0, PolicyEffect.CONFIRM: 1, PolicyEffect.ALLOW: 2}[
                r.effect
            ],
        )
        for rule in ordered:
            ok, reason = self._rule_satisfied(
                rule, method=method, url=url, actor=actor, request_origin=request_origin
            )
            if not ok:
                if rule.effect is PolicyEffect.DENY:
                    continue  # a DENY rule that doesn't match changes nothing
                return PolicyDecision(
                    decision=PolicyEffect.DENY,
                    reason_code=reason,
                    policy_version=self._version,
                    matched_rule_id=rule.id,
                    constraints=_constraints_of(rule),
                    evaluated_at=now,
                )
            return PolicyDecision(
                decision=rule.effect,
                reason_code=ReasonCode.RULE_MATCH,
                policy_version=self._version,
                matched_rule_id=rule.id,
                constraints=_constraints_of(rule),
                evaluated_at=now,
            )
        return PolicyDecision(
            decision=PolicyEffect.DENY,
            reason_code=ReasonCode.NO_MATCH,
            policy_version=self._version,
            matched_rule_id=None,
            evaluated_at=now,
        )

    def _rule_satisfied(
        self,
        rule: PermissionRule,
        *,
        method: str,
        url: str,
        actor: str,
        request_origin: str | None,
    ) -> tuple[bool, ReasonCode]:
        if method not in rule.methods:
            return False, ReasonCode.CONSTRAINT_FAILED
        if rule.actor is not None and rule.actor != actor:
            return False, ReasonCode.CONSTRAINT_FAILED
        if rule.require_origin is not None and rule.require_origin != request_origin:
            # Memory writes without a verifiable user_explicit_command origin
            # (or any forged origin) never reach an executor.
            return False, ReasonCode.CONSTRAINT_FAILED
        if method not in rule.methods:
            return False, ReasonCode.CONSTRAINT_FAILED
        try:
            parts = urlparse(url)
        except ValueError:
            return False, ReasonCode.INVALID_DESTINATION
        if not parts.path.startswith(rule.path_prefix):
            return False, ReasonCode.CONSTRAINT_FAILED
        if rule.loopback_only:
            ok, _ = validate_destination(url)
            if not ok:
                return False, ReasonCode.INVALID_DESTINATION
        return True, ReasonCode.RULE_MATCH


def _constraints_of(rule: PermissionRule) -> PolicyConstraints:
    return PolicyConstraints(
        methods=list(rule.methods),
        path_prefix=rule.path_prefix,
        loopback_only=rule.loopback_only,
        follow_redirects=rule.follow_redirects,
        max_response_bytes=rule.max_response_bytes,
        deadline_ms=rule.deadline_ms,
    )


# Re-export for convenience at the security layer.
__all__ = [
    "Capability",
    "PolicyEngine",
    "ToolName",
    "ToolRequest",
    "validate_destination",
]
