"""Shared contract primitives for the JARVIS domain layer.

The domain layer is pure: it must never import FastAPI, SQLAlchemy, HTTPX,
LLM SDKs, or NEXUS code. Pydantic is the contract language.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    """Timezone-aware UTC now. All domain timestamps are tz-aware UTC."""
    return datetime.now(UTC)


def new_id() -> str:
    """New random v4 UUID as 32-char hex. Carries no temporal/personal info."""
    return uuid.uuid4().hex


class StrictModel(BaseModel):
    """Mutable-but-strict internal contract: rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class FrozenModel(BaseModel):
    """Immutable value object: rejects unknown fields, hashable where possible."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TolerantModel(BaseModel):
    """External boundary DTO: tolerates additive fields from NEXUS.

    Only valid at the adapter boundary. The core never receives these;
    the adapter converts them into strict canonical contracts.
    """

    model_config = ConfigDict(extra="ignore")


class EvidenceRef(FrozenModel):
    """Provenance for one external call. No hosts with credentials, no headers,
    no raw bodies in audit — only method, versioned path, status, time, digest."""

    method: str = Field(pattern=r"^[A-Z]{3,7}$")
    path: str = Field(min_length=1, max_length=512)
    http_status: int = Field(ge=100, le=599)
    received_at: AwareDatetime = Field(default_factory=utcnow)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
