"""Pre-persistence secret scanning for memory writes (design C4).

scan_for_secrets runs BEFORE anything is persisted. A hit denies the write:
nothing is stored, no secret value ever reaches the audit log (only the
pattern category), and there is no override in v1 — the user must remove the
secret from the text and retry.

The pattern set intentionally mirrors the redaction keyword list in
jarvis.security.redaction: anything that would be redacted on the way out is
blocked on the way into memory.
"""

from __future__ import annotations

import re

from ..domain.contracts.common import FrozenModel

# (category, compiled pattern). Patterns match values, never capture them;
# scan_for_secrets returns categories only.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9\-_]{20,}")),
    (
        "github_token",
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*\S{8,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{12,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[-_]?key|client_secret|auth_token|access_token"
            r"|session_token|secret|password|passwd|pwd|token|credentials)\b"
            r"\s*[:=]\s*\S{3,}"
        ),
    ),
)


class SecretHit(FrozenModel):
    """A detected secret pattern. The category is logged; the matched value
    is never stored, returned, or included in any audit summary."""

    category: str


def scan_for_secrets(text: str) -> list[SecretHit]:
    """Scan text for credential-like patterns. Returns one hit per matched
    category (order = pattern table order). Pure function, no I/O."""
    hits: list[SecretHit] = []
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            hits.append(SecretHit(category=category))
    return hits
