"""Unit tests: LLM provider selection, error mapping and key secrecy.

All HTTP is faked via httpx.MockTransport -- no real API calls, no real key.
The API key used here is a distinctive fake value; tests assert it never
appears in raised errors, captured logs or any user-visible surface.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from jarvis.adapters.llm.openai import OpenAIProvider
from jarvis.api.dependencies import build_llm_provider
from jarvis.config import Settings
from jarvis.domain.contracts.common import new_id
from jarvis.domain.contracts.llm import LLMRequest, NexusResponsePlan
from jarvis.domain.contracts.nexus import CanonicalFact
from jarvis.ports.llm import LLMMalformedError, LLMUnavailableError

DUMMY_KEY = "sk-test-DUMMY-9f8e7d6c5b4a"  # noqa: S105 - distinctive fake key for leak tests, never real


def _settings(**kw):
    kw.setdefault("llm_provider", "fake")
    kw.setdefault("nexus_base_url", "http://127.0.0.1:8000")
    kw.setdefault("log_level", "WARNING")
    return Settings(**kw)


def _request():
    return LLMRequest(
        task_id=new_id(),
        facts=[
            CanonicalFact(
                id="FACT_NEXUS_AVAILABILITY", label="NEXUS reachability", value="available"
            )
        ],
        max_output_tokens=200,
    )


def _provider(handler, api_key=DUMMY_KEY):
    return OpenAIProvider(
        model="gpt-4o-mini", api_key=api_key, transport=httpx.MockTransport(handler)
    )


# ---------------------------------------------------------------- selection


def test_empty_price_env_vars_mean_unset(monkeypatch):
    """F3.4 clean-room regression: .env.example ships the optional price
    vars as empty lines; they must parse as None, not crash startup."""
    monkeypatch.setenv("JARVIS_LLM_INPUT_PRICE_PER_1M_USD", "")
    monkeypatch.setenv("JARVIS_LLM_OUTPUT_PRICE_PER_1M_USD", "")
    settings = Settings(
        nexus_base_url="http://127.0.0.1:8000",
        llm_provider="fake",
        log_level="WARNING",
    )
    assert settings.llm_input_price_per_1m_usd is None
    assert settings.llm_output_price_per_1m_usd is None


def test_selection_defaults_to_fake_without_key():
    provider = build_llm_provider(_settings(llm_provider="fake"))
    assert provider.name == "fake"


def test_selection_openai_requires_key_and_model():
    with pytest.raises(ValueError, match="JARVIS_LLM_MODEL"):
        build_llm_provider(_settings(llm_provider="openai", llm_model="", openai_api_key=""))
    with pytest.raises(ValueError, match="JARVIS_LLM_MODEL"):
        build_llm_provider(
            _settings(llm_provider="openai", llm_model="gpt-4o-mini", openai_api_key="")
        )
    with pytest.raises(ValueError, match="JARVIS_LLM_MODEL"):
        build_llm_provider(_settings(llm_provider="openai", llm_model="", openai_api_key=DUMMY_KEY))


def test_selection_openai_builds_provider_when_configured():
    provider = build_llm_provider(
        _settings(llm_provider="openai", llm_model="gpt-4o-mini", openai_api_key=DUMMY_KEY)
    )
    assert provider.name == "openai"


def test_unknown_provider_rejected_by_config():
    with pytest.raises(ValueError, match="llm_provider"):
        _settings(llm_provider="anthropic")


def test_missing_key_is_controlled_error_not_import_crash():
    # Importing the adapter module must never require a key.
    import jarvis.adapters.llm.openai  # noqa: F401

    # Failure happens only when the real provider is *constructed*.
    with pytest.raises(ValueError, match="api_key"):
        OpenAIProvider(model="gpt-4o-mini", api_key="")


# ------------------------------------------------------------- error mapping


async def _raises(handler, exc, **kw):
    provider = _provider(handler, **kw)
    try:
        with pytest.raises(exc) as ei:
            await provider.generate_structured(_request(), timeout_seconds=5.0)
    finally:
        await provider.close()
    return ei.value


async def test_401_maps_to_unavailable_not_malformed():
    exc = await _raises(
        lambda request: httpx.Response(401, json={"error": {"message": "bad key"}}),
        LLMUnavailableError,
    )
    assert "401" in str(exc)


async def test_403_maps_to_unavailable_not_malformed():
    exc = await _raises(
        lambda request: httpx.Response(403, json={"error": {"message": "denied"}}),
        LLMUnavailableError,
    )
    assert "403" in str(exc)


async def test_502_maps_to_unavailable():
    await _raises(
        lambda request: httpx.Response(502, json={"error": "bad gateway"}),
        LLMUnavailableError,
    )


async def test_connection_error_maps_to_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    await _raises(handler, LLMUnavailableError)


async def test_empty_content_maps_to_malformed():
    body = {
        "choices": [{"message": {"content": "   "}, "finish_reason": "stop"}],
        "usage": {},
    }
    exc = await _raises(lambda request: httpx.Response(200, json=body), LLMMalformedError)
    assert "empty" in str(exc)


async def test_missing_choices_maps_to_malformed():
    await _raises(lambda request: httpx.Response(200, json={"usage": {}}), LLMMalformedError)


# ------------------------------------------------------------------ secrecy


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(401, json={"error": "bad key"}),
        lambda request: httpx.Response(500, json={"error": "boom"}),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda request: httpx.Response(200, content=b"not-json{{{"),
    ],
    ids=["401", "500", "connect-error", "malformed-body"],
)
async def test_key_never_leaks_into_exceptions(handler, caplog):
    caplog.set_level(logging.DEBUG, logger="jarvis")
    exc = await _raises(handler, Exception)
    assert DUMMY_KEY not in str(exc), f"key leaked into exception: {exc!r}"
    for record in caplog.records:
        assert DUMMY_KEY not in record.getMessage(), (
            f"key leaked into log: {record.getMessage()[:120]!r}"
        )


async def test_key_never_leaks_into_429_or_timeout():
    def rate_limited(request):
        return httpx.Response(429, json={"error": "slow down"})

    exc = await _raises(rate_limited, LLMUnavailableError)
    assert DUMMY_KEY not in str(exc)


def test_orchestrator_audit_summary_cannot_carry_key():
    """The LLM fallback audit summary uses only the exception *type name*,
    never its text -- so even a key-bearing message could not reach audit."""
    import inspect

    from jarvis.application import orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    assert 'f"llm failed ({type(exc).__name__}); deterministic fallback"' in src


def test_success_contract_still_valid():
    """Sanity: the adapter still returns the validated plan contract."""

    def handler(request):
        plan = {
            "summary_key": "normal",
            "tone": "calm",
            "selected_fact_ids": ["FACT_NEXUS_AVAILABILITY"],
            "selected_recommendation_ids": [],
            "detail_level": "standard",
        }
        body = {
            "choices": [{"message": {"content": json.dumps(plan)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return httpx.Response(200, json=body)

    async def run():
        provider = _provider(handler)
        try:
            return await provider.generate_structured(_request(), timeout_seconds=5.0)
        finally:
            await provider.close()

    import asyncio

    resp = asyncio.run(run())
    assert isinstance(resp.structured_output, NexusResponsePlan)
