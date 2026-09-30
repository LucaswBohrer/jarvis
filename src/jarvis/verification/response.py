"""Post-LLM grounding verification.

The LLM produces a NexusResponsePlan. Before the deterministic renderer may
use it, this module checks:
  1. every selected_fact_id exists in the verified snapshot;
  2. every selected_recommendation_id exists in the verified recommendations;
  3. the plan's summary_key matches the key derived deterministically from
     the verified facts (the LLM may not relabel a critical state as normal).

Any violation -> GroundingError -> the orchestrator uses the audited
deterministic fallback. The LLM never gets to invent or relabel facts.
"""

from __future__ import annotations

from ..domain.contracts.llm import NexusResponsePlan, SummaryKey
from ..domain.contracts.nexus import VerifiedNexusStatus


class GroundingError(Exception):
    """The LLM plan references unknown facts or contradicts verified state."""


def fact_str(value: object) -> str:
    """Canonical string form of a fact value for derivation/comparison."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def derive_summary_key(facts: dict[str, str]) -> SummaryKey:
    """Deterministic summary key from verified fact strings. Priority:
    UNAVAILABLE > ERROR > EMPTY > STALE > SIMULATION > CRITICAL > WARNING > NORMAL.
    """
    if facts.get("FACT_NEXUS_AVAILABILITY") == "unavailable":
        return SummaryKey.UNAVAILABLE
    state = facts.get("FACT_DATA_STATE")
    if state == "invalid":
        return SummaryKey.ERROR
    if state == "empty":
        return SummaryKey.EMPTY
    if state == "stale":
        return SummaryKey.STALE
    if facts.get("FACT_SIMULATION_RUNNING") == "true":
        return SummaryKey.SIMULATION
    electrical = facts.get("FACT_ELECTRICAL_STATUS")
    if electrical == "critical":
        return SummaryKey.CRITICAL
    if electrical == "warning":
        return SummaryKey.WARNING
    return SummaryKey.NORMAL


def verify_plan(plan: NexusResponsePlan, verified: VerifiedNexusStatus) -> NexusResponsePlan:
    """Validate an LLM plan against the verified snapshot. Returns the plan
    unchanged when grounded; raises GroundingError otherwise."""
    fact_ids = {f.id for f in verified.facts}
    unknown_facts = [fid for fid in plan.selected_fact_ids if fid not in fact_ids]
    if unknown_facts:
        raise GroundingError(f"plan references unknown fact ids: {unknown_facts}")
    rec_ids = {r.id for r in verified.recommendations}
    unknown_recs = [rid for rid in plan.selected_recommendation_ids if rid not in rec_ids]
    if unknown_recs:
        raise GroundingError(f"plan references unknown recommendation ids: {unknown_recs}")
    fact_map = {f.id: fact_str(f.value) for f in verified.facts}
    expected = derive_summary_key(fact_map)
    if plan.summary_key is not expected:
        raise GroundingError(
            f"plan summary_key {plan.summary_key.value!r} contradicts verified "
            f"facts (expected {expected.value!r})"
        )
    return plan
