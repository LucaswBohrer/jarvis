"""JARVIS Phase 1 configuration. Local-only is validated, not just documented.

- JARVIS_HOST must be loopback (0.0.0.0 / LAN addresses rejected);
- JARVIS_NEXUS_BASE_URL must be http(s), no userinfo, loopback-only destination;
- JARVIS_DATABASE_URL must be a local sqlite file that never references NEXUS;
- provider=openai requires key+model; provider=fake is for explicit test/dev.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import unquote, urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .security.policy import validate_destination

JARVIS_VERSION = "0.1.0"
SCHEMA_HEAD = "0004"


def references_nexus_database(url: str) -> bool:
    """True if a sqlite URL path touches a "nexus" directory or database file.

    Matches on whole path components, not substrings (D22): a plain
    substring check false-positives on e.g. pytest tmp dirs named after a
    test containing the word "nexus".
    """
    path = unquote(urlsplit(url).path).lower()
    components = [c for c in path.split("/") if c]
    return any(c == "nexus" or c.startswith("nexus.") for c in components)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file=".env", extra="ignore")

    env: str = "local"
    host: str = "127.0.0.1"
    port: int = 8123
    database_url: str = "sqlite+aiosqlite:///./data/jarvis.db"
    log_level: str = "INFO"
    log_format: str = "json"
    default_locale: str = "pt-BR"
    policy_file: str = "./config/policy.toml"

    nexus_base_url: str = "http://127.0.0.1:8000"
    nexus_equipment_code: str = "DEFAULT"
    nexus_connect_timeout_ms: int = 400
    nexus_read_timeout_ms: int = 1200
    nexus_pool_timeout_ms: int = 400
    nexus_total_deadline_ms: int = 3500
    nexus_max_retries: int = 1
    nexus_circuit_failure_threshold: int = 3
    nexus_circuit_reset_seconds: int = 15
    nexus_max_response_bytes: int = 262144
    nexus_freshness_seconds: int = 10

    llm_provider: str = "fake"
    llm_model: str = ""
    llm_timeout_seconds: float = 15.0
    llm_max_output_tokens: int = 300
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_input_price_per_1m_usd: float | None = None
    llm_output_price_per_1m_usd: float | None = None

    # Context budgets in chars (Phase 2, Slice 2, §8.3). No token counting.
    ctx_total_chars: int = 12000
    ctx_tail_chars: int = 4000
    ctx_preferences_chars: int = 2000
    ctx_memories_chars: int = 4000
    ctx_facts_chars: int = 8000

    @field_validator("host")
    @classmethod
    def _host_loopback(cls, value: str) -> str:
        ok, reason = validate_destination(f"http://{value}/")
        if not ok:
            raise ValueError(f"JARVIS_HOST must be loopback: {reason}")
        return value

    @field_validator("port")
    @classmethod
    def _port_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port out of range")
        return value

    @field_validator("nexus_base_url")
    @classmethod
    def _nexus_loopback(cls, value: str) -> str:
        ok, reason = validate_destination(value)
        if not ok:
            raise ValueError(f"JARVIS_NEXUS_BASE_URL invalid: {reason}")
        return value.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def _db_local(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite:///"):
            raise ValueError("JARVIS_DATABASE_URL must be a local sqlite+aiosqlite URL")
        if references_nexus_database(value):
            raise ValueError("JARVIS_DATABASE_URL must never reference NEXUS")
        return value

    @field_validator("llm_provider")
    @classmethod
    def _provider_known(cls, value: str) -> str:
        if value not in ("fake", "openai"):
            raise ValueError("llm_provider must be 'fake' or 'openai'")
        return value

    @field_validator("nexus_connect_timeout_ms", "nexus_read_timeout_ms", "nexus_pool_timeout_ms")
    @classmethod
    def _timeout_bounds(cls, value: int) -> int:
        if not 50 <= value <= 10_000:
            raise ValueError("timeout must be within 50..10000 ms")
        return value

    @field_validator("nexus_total_deadline_ms")
    @classmethod
    def _deadline_bounds(cls, value: int) -> int:
        if not 500 <= value <= 30_000:
            raise ValueError("total deadline must be within 500..30000 ms")
        return value

    @field_validator("nexus_max_response_bytes")
    @classmethod
    def _body_bounds(cls, value: int) -> int:
        if not 1024 <= value <= 10 * 1024 * 1024:
            raise ValueError("max response bytes out of bounds")
        return value

    @field_validator("nexus_freshness_seconds")
    @classmethod
    def _freshness_bounds(cls, value: int) -> int:
        if not 1 <= value <= 3600:
            raise ValueError("freshness seconds out of bounds")
        return value

    @field_validator("llm_timeout_seconds")
    @classmethod
    def _llm_timeout_bounds(cls, value: float) -> float:
        if not 1.0 <= value <= 120.0:
            raise ValueError("llm timeout out of bounds")
        return value

    @field_validator(
        "ctx_total_chars",
        "ctx_tail_chars",
        "ctx_preferences_chars",
        "ctx_memories_chars",
        "ctx_facts_chars",
    )
    @classmethod
    def _ctx_budget_bounds(cls, value: int) -> int:
        if not 100 <= value <= 100_000:
            raise ValueError("context budget out of bounds (100..100000 chars)")
        return value

    def validate_provider_ready(self) -> None:
        """Fail fast when the configured provider cannot run."""
        if self.llm_provider == "openai" and (not self.openai_api_key or not self.llm_model):
            raise ValueError(
                "llm_provider=openai requires JARVIS_LLM_MODEL and JARVIS_OPENAI_API_KEY"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests pass explicit Settings to create_app() instead."""
    return Settings()
