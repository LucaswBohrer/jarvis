"""Unit tests: OpenAI LLM provider.

All HTTP is faked via httpx.MockTransport -- no real API calls, no key
needed. Covers the §31 repeat items: timeout, cancellation, malformed
output, usage/cost metrics, structured-output payload shape.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis.adapters.llm.openai import OpenAIProvider
from jarvis.domain.contracts.common import new_id
from jarvis.domain.contracts.llm import LLMRequest, NexusResponsePlan
from jarvis.domain.contracts.nexus import CanonicalFact
from jarvis.ports.llm import LLMMalformedError, LLMTimeoutError, LLMUnavailableError


def _request():
    return LLMRequest(
        task_id=new_id(),
        facts=[
            CanonicalFact(
                id="FACT_NEXUS_AVAILABILITY", label="NEXUS reachability", value="available"
            ),
            CanonicalFact(id="FACT_ELECTRICAL_STATUS", label="Electrical status", value="normal"),
        ],
        max_output_tokens=200,
    )


def _ok_body(**over):
    plan = {
        "summary_key": "normal",
        "tone": "calm",
        "selected_fact_ids": ["FACT_NEXUS_AVAILABILITY", "FACT_ELECTRICAL_STATUS"],
        "selected_recommendation_ids": [],
        "detail_level": "standard",
    }
    body = {
        "choices": [{"message": {"content": json.dumps(plan)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
    }
    body.update(over)
    return body


def _provider(handler, **kw):
    kw.setdefault("input_price_per_1m_usd", 2.5)
    kw.setdefault("output_price_per_1m_usd", 10.0)
    return OpenAIProvider(
        model="gpt-4o-mini",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kw,
    )


async def test_success_returns_validated_plan_and_usage():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_ok_body())

    provider = _provider(handler)
    try:
        resp = await provider.generate_structured(_request(), timeout_seconds=5.0)
    finally:
        await provider.close()
    assert isinstance(resp.structured_output, NexusResponsePlan)
    assert resp.structured_output.summary_key.value == "normal"
    assert resp.usage.input_tokens == 120
    assert resp.usage.output_tokens == 40
    # cost: 120/1e6*2.5 + 40/1e6*10 = 0.0007
    assert resp.usage.estimated_cost_usd == pytest.approx(0.0007)
    # structured output requested
    rf = seen["payload"]["response_format"]
    assert rf["type"] == "json_schema"
    assert "selected_fact_ids" in rf["json_schema"]["schema"]["properties"]
    assert seen["payload"]["temperature"] == 0.0


async def test_timeout_maps_to_llm_timeout_error():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    provider = _provider(handler)
    try:
        with pytest.raises(LLMTimeoutError):
            await provider.generate_structured(_request(), timeout_seconds=1.0)
    finally:
        await provider.close()


async def test_rate_limit_and_server_errors_map_to_unavailable():
    for status in (429, 500, 503):

        def handler(request, status=status):
            return httpx.Response(status, json={"error": "x"})

        provider = _provider(handler)
        try:
            with pytest.raises(LLMUnavailableError):
                await provider.generate_structured(_request(), timeout_seconds=5.0)
        finally:
            await provider.close()


async def test_malformed_json_maps_to_malformed_error():
    def handler(request):
        return httpx.Response(200, content=b"not-json{{{")

    provider = _provider(handler)
    try:
        with pytest.raises(LLMMalformedError):
            await provider.generate_structured(_request(), timeout_seconds=5.0)
    finally:
        await provider.close()


async def test_plan_violating_schema_maps_to_malformed_error():
    def handler(request):
        plan = {"summary_key": "normal", "tone": "calm", "selected_fact_ids": []}
        body = _ok_body()
        body["choices"][0]["message"]["content"] = json.dumps(plan)
        return httpx.Response(200, json=body)

    provider = _provider(handler)
    try:
        with pytest.raises(LLMMalformedError):
            await provider.generate_structured(_request(), timeout_seconds=5.0)
    finally:
        await provider.close()


async def test_cancellation_propagates_untouched():
    async def handler(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=_ok_body())

    provider = _provider(handler)
    try:
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                provider.generate_structured(_request(), timeout_seconds=60.0)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            await task
    finally:
        await provider.close()


async def test_health_false_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("down")

    provider = _provider(handler)
    try:
        assert await provider.health() is False
    finally:
        await provider.close()


def test_construction_requires_model_and_key():
    with pytest.raises(ValueError):
        OpenAIProvider(model="", api_key="k")
    with pytest.raises(ValueError):
        OpenAIProvider(model="m", api_key="")


def test_construction_survives_malformed_proxy_env(monkeypatch):
    """D23: malformed proxy env (this machine's no_proxy) must not crash
    provider construction; it falls back to a direct connection."""
    monkeypatch.setenv("NO_PROXY", "localhost,::1,[::1]")
    provider = OpenAIProvider(model="m", api_key="k")
    assert provider.name == "openai"
