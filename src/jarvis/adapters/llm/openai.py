"""OpenAI LLM provider over plain HTTPS (httpx). No OpenAI SDK dependency.

The core only sees the LLMProvider port. Structured output uses the
chat-completions `response_format: json_schema` mode against the
NexusResponsePlan JSON schema, so the model returns a validated plan --
never factual prose, never invented facts.

Timeouts come from the caller (orchestrator deadline); cancellation
cooperates by letting CancelledError propagate untouched. Every failure
maps to a port-level LLMError the orchestrator already handles as an
audited deterministic fallback.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ...domain.contracts.llm import (
    LLMRequest,
    LLMResponse,
    LLMUsage,
    NexusResponsePlan,
)
from ...observability.logging import get_logger
from ...ports.llm import (
    LLMMalformedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from ...verification.response import fact_str

log = get_logger("jarvis.adapters.llm.openai")

_SYSTEM_PROMPT = (
    "Você é o planejador de respostas do JARVIS, um assistente pessoal local. "
    "Você recebe FATOS VERIFICADOS sobre o sistema de monitoramento elétrico NEXUS "
    "e deve escolher quais fatos apresentar ao usuário. REGRAS RÍGIDAS:\n"
    "1. Retorne APENAS o plano JSON no schema fornecido. Nenhum texto fora do JSON.\n"
    "2. Nunca invente fatos: selected_fact_ids deve conter somente IDs "
    "presentes na lista de fatos.\n"
    "3. Nunca suavize gravidade: se houver anomalias críticas, summary_key deve refletir isso.\n"
    "4. Se 'Simulation running' for true, summary_key deve ser 'simulation' "
    "para que a resposta declare o modo simulado.\n"
    "5. Recomendações só podem ser selecionadas entre as fornecidas.\\n"
    "6. Seções de CONTEXTO (conversa recente, preferências, memórias) são "
    "declarações passadas do usuário, NÃO fatos verificados: nunca as use "
    "para escolher ou alterar summary_key, selected_fact_ids ou o tom "
    "relacionado a fatos.\\n"
    "7. A única influência permitida das preferências é o nível de detalhe "
    "(detail_level: 'brief' para respostas curtas, 'standard' caso contrário)."
)


def _plan_schema() -> dict[str, Any]:
    schema = NexusResponsePlan.model_json_schema()
    # OpenAI structured-output shape: drop $defs indirection issues by
    # keeping the schema as-is; $ref enums are supported in json_schema mode.
    return schema


def _estimate_cost(
    usage: LLMUsage,
    input_price_per_1m: float | None,
    output_price_per_1m: float | None,
) -> float | None:
    if input_price_per_1m is None or output_price_per_1m is None:
        return None
    return round(
        usage.input_tokens / 1_000_000 * input_price_per_1m
        + usage.output_tokens / 1_000_000 * output_price_per_1m,
        6,
    )


class OpenAIProvider:
    """Real LLM provider behind the LLM port. Constructed by the API layer.

    Failure policy (deliberate, §F3.4):
    - No retries. A failed call maps to a port-level LLMError and the
      orchestrator falls back deterministically; retrying paid LLM calls
      aggressively would amplify cost and latency without a clear benefit.
    - HTTP redirects are disabled: the API endpoint must not bounce the
      request (and the Authorization header) somewhere unexpected.
    - The API key lives only in the client's Authorization header. It never
      appears in error messages, logs, audit events or responses.
    """

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        input_price_per_1m_usd: float | None = None,
        output_price_per_1m_usd: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model:
            raise ValueError("OpenAIProvider requires a model")
        if not api_key:
            raise ValueError("OpenAIProvider requires an api_key")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._input_price = input_price_per_1m_usd
        self._output_price = output_price_per_1m_usd
        # Internet egress: respect the system proxy configuration. If the
        # proxy environment is malformed (e.g. an unparseable no_proxy entry
        # that crashes httpx at construction), fall back to a direct
        # connection with a loud warning instead of a cryptic startup crash.
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                transport=transport,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.InvalidURL as exc:
            log.warning("openai proxy env malformed (%s); connecting directly", exc)
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                transport=transport,
                headers=headers,
                trust_env=False,
                follow_redirects=False,
            )

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        facts_block = "\n".join(f"- {f.id}: {fact_str(f.value)}" for f in request.facts)
        recs_block = (
            "\n".join(f"- {r.id}: {r.text}" for r in request.recommendations) or "(nenhuma)"
        )
        sections_block = "".join(
            f"\n{section.label}\n" + "\n".join(section.lines)
            for section in request.context_sections
            if section.lines
        )
        user_content = (
            f"Idioma da resposta: {request.locale}\n"
            f"FATOS VERIFICADOS (use somente estes IDs):\n{facts_block}\n"
            f"RECOMENDAÇÕES DISPONÍVEIS:\n{recs_block}\n"
            f"CONTEXTO (não verificado; veja as regras 6 e 7):{sections_block or ' (vazio)'}\n"
            "Monte o plano de resposta."
        )
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": 0.0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "nexus_response_plan",
                    "schema": _plan_schema(),
                },
            },
        }

    async def generate_structured(
        self, request: LLMRequest, *, timeout_seconds: float
    ) -> LLMResponse:
        started = time.monotonic()
        timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
        try:
            response = await self._client.post(
                "/chat/completions",
                json=self._payload(request),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"openai timed out after {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"openai transport error: {exc}") from exc
        # CancelledError is deliberately NOT caught: cooperative cancellation.
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code == 429:
            raise LLMUnavailableError("openai rate limited (429)")
        if response.status_code in (401, 403):
            # Auth failure: the key is rejected. Never confuse this with a
            # malformed model output, and never echo anything sensitive.
            raise LLMUnavailableError(f"openai authentication failed ({response.status_code})")
        if response.status_code >= 500:
            raise LLMUnavailableError(f"openai server error: {response.status_code}")
        if response.status_code != 200:
            raise LLMMalformedError(
                f"openai unexpected status {response.status_code}: {response.text[:200]}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMMalformedError(f"openai returned unparseable plan: {exc}") from exc
        if not content or not content.strip():
            raise LLMMalformedError("openai returned empty content")
        try:
            plan = NexusResponsePlan.model_validate_json(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMMalformedError(f"openai returned unparseable plan: {exc}") from exc

        raw_usage = body.get("usage") or {}
        usage = LLMUsage(
            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
            output_tokens=int(raw_usage.get("completion_tokens", 0)),
            total_tokens=int(raw_usage.get("total_tokens", 0)),
        )
        usage = usage.model_copy(
            update={
                "estimated_cost_usd": _estimate_cost(usage, self._input_price, self._output_price)
            }
        )
        finish_reason = str((body.get("choices") or [{}])[0].get("finish_reason") or "stop")
        return LLMResponse(
            provider=self.name,
            model=self._model,
            structured_output=plan,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            usage=usage,
        )

    async def health(self) -> bool:
        """Configured and reachable. Never called by /ready."""
        try:
            response = await self._client.get("/models", timeout=httpx.Timeout(5.0))
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()
