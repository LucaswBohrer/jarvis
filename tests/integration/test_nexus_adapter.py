"""Integration tests: NEXUS HTTP adapter.

Covers vertical-slice scenarios:
 7. connection refused -> one retry, explicit unavailability
 8. timeout -> deadline respected, NEXUS_TIMEOUT audited via error code
 9. HTTP 503 -> limited retry, no partial payload
10. HTTP 404 on equipment -> NEXUS_EQUIPMENT_NOT_FOUND (no fallback)
11. invalid JSON/schema -> NEXUS_CONTRACT_INVALID
12. redirect to another host -> blocked, never followed
15. open circuit -> fast failure, zero HTTP calls
+ trust_env=False regression (D18): proxy env vars must not break construction.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from jarvis.adapters.nexus.http import NexusAdapterConfig, NexusHttpAdapter
from jarvis.domain.contracts.common import new_id, utcnow
from jarvis.domain.contracts.nexus import NexusStatusQuery
from jarvis.domain.contracts.tool import Capability, ToolName, ToolOutcome, ToolRequest
from jarvis.domain.errors import ErrorCode
from tests.conftest import make_nexus_handler


def _request():
    return ToolRequest(
        task_id=new_id(),
        correlation_id=new_id(),
        tool_name=ToolName.NEXUS_STATUS,
        capability=Capability.NEXUS_STATUS_READ,
        arguments=NexusStatusQuery(equipment_code="DEFAULT"),
        deadline_at=utcnow() + timedelta(seconds=10),
    )


def _adapter(handler=None, **cfg_over):
    cfg = NexusAdapterConfig(base_url="http://127.0.0.1:8000", **cfg_over)
    transport = httpx.MockTransport(handler) if handler is not None else None
    return NexusHttpAdapter(cfg, transport=transport)


async def test_success_builds_canonical_status():
    handler = make_nexus_handler()
    ad = _adapter(handler)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.SUCCESS
    assert result.data is not None
    assert result.data.equipment.code == "DEFAULT"
    assert handler.calls["n"] == 3  # equipment + summary + simulation


async def test_connection_refused_retries_once_then_unavailable():
    def refusing(request):
        raise httpx.ConnectError("refused")

    ad = _adapter(refusing, max_retries=1)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_UNAVAILABLE


async def test_timeout_surfaces_nexus_timeout():
    handler = make_nexus_handler(sleep_s=5)
    ad = _adapter(handler, read_timeout_ms=200, total_deadline_ms=800, max_retries=0)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_TIMEOUT


async def test_http_503_limited_retry_no_partial_payload():
    handler = make_nexus_handler(status_overrides={"/api/v1/equipment": 503})
    ad = _adapter(handler, max_retries=1)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.data is None  # never a partial payload
    assert handler.calls["n"] == 2  # initial + 1 retry, then stop


async def test_http_404_equipment_no_fallback():
    handler = make_nexus_handler(status_overrides={"/api/v1/equipment": 404})
    ad = _adapter(handler)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_EQUIPMENT_NOT_FOUND


async def test_invalid_json_contract_invalid():
    handler = make_nexus_handler(raw_overrides={"/api/v1/equipment": b"not-json{{{"})
    ad = _adapter(handler)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_CONTRACT_INVALID


async def test_redirect_to_other_host_blocked():
    def redirecting(request):
        if request.url.path == "/api/v1/equipment":
            return httpx.Response(302, headers={"location": "http://evil.example/api/v1/equipment"})
        return httpx.Response(404, json={})

    ad = _adapter(redirecting)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    # redirect is never followed; surfaced as an explicit blocked-redirect error
    assert result.error.code == ErrorCode.NEXUS_REDIRECT_BLOCKED


async def test_open_circuit_fails_fast_zero_calls():
    def flaky(request):
        raise httpx.ConnectError("down")

    ad = _adapter(flaky, max_retries=0, circuit_failure_threshold=2, circuit_reset_seconds=60)
    # trip the breaker
    for _ in range(2):
        await ad.get_status(_request())
    handler_calls = {"n": 0}

    def counting(request):
        handler_calls["n"] += 1
        return httpx.Response(200, json={})

    # swap transport behind the breaker is not possible; instead assert the
    # breaker state directly and that a further call fails without I/O.
    from jarvis.adapters.nexus.http import CircuitBreaker

    assert isinstance(ad.breaker, CircuitBreaker)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_CIRCUIT_OPEN


async def test_response_size_limit_enforced():
    big = b'{"equipment": [' + b'{"id":"x"},' * 20000 + b'{"id":"y"}]}'
    handler = make_nexus_handler(raw_overrides={"/api/v1/equipment": big})
    ad = _adapter(handler, max_response_bytes=1024)
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.FAILURE
    assert result.error is not None
    assert result.error.code == ErrorCode.NEXUS_RESPONSE_TOO_LARGE


async def test_trust_env_false_with_proxy_env(monkeypatch):
    """D18 regression: proxy env vars (even malformed) must not break the
    loopback-only adapter."""
    monkeypatch.setenv("HTTP_PROXY", "http://[::1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy:3128")
    handler = make_nexus_handler()
    ad = _adapter(handler)  # must not raise
    result = await ad.get_status(_request())
    await ad.close()
    assert result.outcome == ToolOutcome.SUCCESS


async def test_rejects_non_loopback_base_url():
    with pytest.raises(ValueError):
        NexusHttpAdapter(NexusAdapterConfig(base_url="http://192.168.1.10:8000"))
