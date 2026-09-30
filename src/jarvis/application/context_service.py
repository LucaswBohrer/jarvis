"""Builds the LLM request from verified facts.

Only facts and recommendations produced by the verification pipeline are
included. Raw NEXUS payloads never reach the LLM.

Phase 2, Slice 2: the request also carries labeled context sections
(conversation tail, preferences, memories) assembled by the ContextBuilder.
Sensitive memories are excluded from the sections before this point.
"""

from __future__ import annotations

from ..domain.contracts.context import ContextSection
from ..domain.contracts.llm import LLMRequest
from ..domain.contracts.nexus import VerifiedNexusStatus


def build_llm_request(
    verified: VerifiedNexusStatus,
    *,
    task_id: str,
    locale: str = "pt-BR",
    max_output_tokens: int = 300,
    context_sections: tuple[ContextSection, ...] = (),
) -> LLMRequest:
    return LLMRequest(
        task_id=task_id,
        facts=verified.facts,
        recommendations=verified.recommendations,
        locale=locale,
        max_output_tokens=max_output_tokens,
        context_sections=context_sections,
    )
