"""Central secret redaction. Applied before anything reaches audit or logs.

Rules:
  - mapping keys containing a sensitive token (case-insensitive substring)
    have their values replaced with "[REDACTED]";
  - URLs are stripped of userinfo;
  - Authorization/Cookie-style headers are dropped entirely.
Redaction is recursive over dicts, lists, tuples, and sets.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_SUBSTRINGS = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "bearer",
    "cookie",
    "set-cookie",
    "credentials",
    "private_key",
    "client_secret",
    "session_token",
)


def _sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered in ("token", "secret", "password", "authorization", "cookie"):
        return True
    return any(token in lowered for token in _SENSITIVE_SUBSTRINGS)


def redact(obj: Any) -> Any:
    """Recursively redact sensitive values. Returns a redacted copy."""
    if isinstance(obj, dict):
        return {
            key: REDACTED if _sensitive_key(str(key)) else redact(value)
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        redacted = [redact(item) for item in obj]
        return type(obj)(redacted) if isinstance(obj, tuple) else redacted
    if isinstance(obj, (set, frozenset)):
        return type(obj)(redact(item) for item in obj)
    return obj


def redact_url(url: str) -> str:
    """Remove userinfo (and only userinfo) from a URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop auth-ish headers entirely; keep the rest."""
    return {name: value for name, value in headers.items() if not _sensitive_key(name)}


def summarize_request(tool_name: str, capability: str, arguments: dict[str, Any]) -> str:
    """Short, safe one-line summary for audit request_summary fields."""
    safe_args = redact(arguments)
    rendered = ", ".join(f"{k}={v}" for k, v in safe_args.items())
    text = f"{tool_name} {capability} {rendered}".strip()
    return text[:512]
