"""Deterministic pt-BR renderer for the NEXUS status vertical slice.

The renderer never calls the LLM and never invents text: it fills fixed
templates with values from the selected verified facts only. Simulation,
stale, empty, invalid, and unavailable states are declared explicitly.

Also provides the audited deterministic fallback plan used when the LLM is
slow, malformed, or contradicts verified state.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from ..domain.contracts.commitments import Commitment
from ..domain.contracts.context import CommitmentCtx, MemoryCtx
from ..domain.contracts.llm import (
    DetailLevel,
    NexusResponsePlan,
    PlanTone,
    SummaryKey,
)
from ..domain.contracts.memory import MemoryHit, MemoryItem
from ..domain.contracts.nexus import VerifiedNexusStatus
from ..domain.contracts.task import (
    AssistantResponse,
    ResponseSource,
    ResponseStatus,
)
from ..verification.response import derive_summary_key, fact_str

_TONE_BY_KEY = {
    SummaryKey.NORMAL: PlanTone.CALM,
    SummaryKey.WARNING: PlanTone.ATTENTION,
    SummaryKey.CRITICAL: PlanTone.URGENT,
    SummaryKey.STALE: PlanTone.ATTENTION,
    SummaryKey.EMPTY: PlanTone.CALM,
    SummaryKey.UNAVAILABLE: PlanTone.CALM,
    SummaryKey.ERROR: PlanTone.ATTENTION,
    SummaryKey.SIMULATION: PlanTone.CALM,
}


def _num(value: object) -> str:
    """Format a number in pt-BR style (comma decimal separator)."""
    try:
        f = float(str(value))
    except (TypeError, ValueError):
        return str(value)
    text = f"{f:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _fmt_ts(value: object) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d/%m/%Y %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def build_fallback_plan(verified: VerifiedNexusStatus, *, task_id: str) -> NexusResponsePlan:
    """Deterministic plan used when the LLM plan cannot be used.

    Selects the core facts, measurement facts, and recommendations. Deterministic
    and audited as ResponseSource.DETERMINISTIC_FALLBACK.
    """
    facts = {f.id: fact_str(f.value) for f in verified.facts}
    key = derive_summary_key(facts)
    core = [
        "FACT_NEXUS_AVAILABILITY",
        "FACT_DATA_STATE",
        "FACT_EQUIPMENT_NAME",
        "FACT_EQUIPMENT_CODE",
        "FACT_ELECTRICAL_STATUS",
        "FACT_DIAGNOSIS_SEVERITY",
        "FACT_ANOMALY_COUNT",
        "FACT_LAST_READING_AT",
        "FACT_SIMULATION_RUNNING",
        "FACT_SIMULATION_MODE",
    ]
    measurement = [
        "FACT_VOLTAGE_V",
        "FACT_CURRENT_A",
        "FACT_FREQUENCY_HZ",
        "FACT_POWER_FACTOR",
        "FACT_ACTIVE_POWER_W",
        "FACT_TEMPERATURE_C",
    ]
    available = {f.id for f in verified.facts}
    selected = [fid for fid in core + measurement if fid in available]
    selected += [
        fid for fid in available if fid.startswith("FACT_ANOMALY_") and fid != "FACT_ANOMALY_COUNT"
    ]
    return NexusResponsePlan(
        summary_key=key,
        tone=_TONE_BY_KEY[key],
        detail_level=DetailLevel.STANDARD,
        selected_fact_ids=selected,
        selected_recommendation_ids=[r.id for r in verified.recommendations],
    )


_SIMULATION_NOTE = (
    "Atenção: o NEXUS está em modo de simulação — os valores abaixo são "
    "simulados, não medições físicas."
)


def _electrical_line(facts: dict[str, str]) -> str:
    parts: list[str] = []
    if facts.get("FACT_VOLTAGE_V"):
        parts.append(f"{_num(facts['FACT_VOLTAGE_V'])} V")
    if facts.get("FACT_CURRENT_A"):
        parts.append(f"{_num(facts['FACT_CURRENT_A'])} A")
    if facts.get("FACT_FREQUENCY_HZ"):
        parts.append(f"{_num(facts['FACT_FREQUENCY_HZ'])} Hz")
    if facts.get("FACT_POWER_FACTOR"):
        parts.append(f"FP {_num(facts['FACT_POWER_FACTOR'])}")
    if facts.get("FACT_ACTIVE_POWER_W"):
        parts.append(f"{_num(facts['FACT_ACTIVE_POWER_W'])} W")
    if facts.get("FACT_TEMPERATURE_C"):
        parts.append(f"{_num(facts['FACT_TEMPERATURE_C'])} °C")
    return ", ".join(parts)


def _equipment_label(facts: dict[str, str]) -> str:
    name = facts.get("FACT_EQUIPMENT_NAME", "")
    code = facts.get("FACT_EQUIPMENT_CODE", "")
    if name and code and name != code:
        return f"{name} ({code})"
    return name or code or "equipamento"


def render_assistant_response(
    verified: VerifiedNexusStatus,
    plan: NexusResponsePlan,
    *,
    task_id: str,
    source: ResponseSource,
    memory_notes: Sequence[MemoryCtx] = (),
    commitment_notes: Sequence[CommitmentCtx] = (),
) -> AssistantResponse:
    """Fill the fixed template for plan.summary_key from selected facts only.

    memory_notes: optional standard (non-sensitive) memories rendered as a
    trailing "Você me disse:" section (Slice 2, §8.4). The caller filters
    out sensitive memories; this function never sees them.

    commitment_notes: commitments surfaced this interaction, rendered as a
    trailing "📌 Lembretes:" section (Slice 3, §8.4). Presentation only —
    this function performs no action on them.
    """
    facts = {f.id: fact_str(f.value) for f in verified.facts if f.id in plan.selected_fact_ids}
    key = plan.summary_key
    message = _render_for_key(key, facts, verified, plan.selected_recommendation_ids)
    if memory_notes:
        notes = "\n".join(f"• {note.title} — {note.snippet}" for note in memory_notes)
        message = f"{message}\n\nVocê me disse:\n{notes}"
    if commitment_notes:
        message = f"{message}\n\n{render_commitment_reminders(commitment_notes)}"
    return AssistantResponse(
        task_id=task_id,
        source=source,
        status=ResponseStatus(key.value),
        message=message,
        fact_ids=list(plan.selected_fact_ids),
    )


def _render_for_key(
    key: SummaryKey,
    facts: dict[str, str],
    verified: VerifiedNexusStatus,
    selected_rec_ids: list[str],
) -> str:
    if key is SummaryKey.UNAVAILABLE:
        return "Não consigo consultar o NEXUS neste momento. A API local está indisponível."
    if key is SummaryKey.ERROR:
        return (
            "Recebi a resposta do NEXUS, mas ela está incompleta ou fora do "
            "formato esperado, então não vou apresentar nenhum valor. "
            "Tente novamente em instantes."
        )

    eq = _equipment_label(facts)
    electrical = _electrical_line(facts)
    ts = (
        _fmt_ts(facts["FACT_LAST_READING_AT"])
        if facts.get("FACT_LAST_READING_AT")
        else "desconhecida"
    )
    sim_note = f" {_SIMULATION_NOTE}" if facts.get("FACT_SIMULATION_RUNNING") == "true" else ""

    recs = {r.id: r.text for r in verified.recommendations}
    rec_texts = [recs[rid] for rid in selected_rec_ids if rid in recs]

    if key is SummaryKey.EMPTY:
        return (
            f"O NEXUS respondeu, mas ainda não há leituras registradas para {eq}. "
            f"Nenhum dado elétrico para apresentar.{sim_note}"
        )

    if key is SummaryKey.STALE:
        known = facts.get("FACT_ELECTRICAL_STATUS", "desconhecido")
        return (
            f"O NEXUS respondeu, mas a última leitura está desatualizada "
            f"(recebida em {ts}). Último estado conhecido: {known}. "
            f"Não considere este o estado atual.{sim_note}"
        )

    if key is SummaryKey.NORMAL:
        base = f"O NEXUS está normal. {eq}: {electrical}. Última leitura em {ts}."
    elif key is SummaryKey.WARNING:
        sev = facts.get("FACT_DIAGNOSIS_SEVERITY", "indeterminada")
        base = (
            f"O NEXUS está em atenção. {eq}: estado elétrico "
            f"{facts.get('FACT_ELECTRICAL_STATUS', 'warning')} (severidade {sev}). "
            f"{electrical}. Última leitura em {ts}."
        )
    elif key is SummaryKey.CRITICAL:
        sev = facts.get("FACT_DIAGNOSIS_SEVERITY", "indeterminada")
        base = (
            f"O NEXUS está em estado crítico. {eq}: estado elétrico "
            f"{facts.get('FACT_ELECTRICAL_STATUS', 'critical')} (severidade {sev}). "
            f"{electrical}. Última leitura em {ts}."
        )
    elif key is SummaryKey.SIMULATION:
        base = f"O NEXUS está operacional. {eq}: {electrical}. Última leitura em {ts}."
    else:  # pragma: no cover - SummaryKey is exhaustive above
        base = f"Estado do NEXUS: {key.value}."

    parts = [base]
    anomaly_msgs = [
        facts[fid]
        for fid in sorted(facts)
        if fid.startswith("FACT_ANOMALY_") and fid != "FACT_ANOMALY_COUNT" and facts[fid]
    ]
    if anomaly_msgs:
        parts.append("Anomalias: " + "; ".join(anomaly_msgs) + ".")
    if rec_texts:
        parts.append("Recomendações: " + "; ".join(rec_texts) + ".")
    if sim_note:
        parts.append(_SIMULATION_NOTE)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Memory slice renderers (Phase 2, Slice 1). Deterministic templates only:
# the LLM never renders memory content.
# ---------------------------------------------------------------------------


def render_memory_write_response(item: MemoryItem) -> str:
    """Confirmation for an explicit memory write. No content echo beyond the
    title — the full content stays out of the chat rendering path."""
    return f"Guardei na memória: {item.title}"


def render_memory_read_response(query_text: str, hits: list[MemoryHit]) -> str:
    """Render retrieval hits as a "você me disse" section. Empty query text
    means a list-all request."""
    if not hits:
        if query_text:
            return f'Não encontrei nada na memória sobre "{query_text}".'
        return "Sua memória está vazia."
    lines = ["Você me disse:"]
    for hit in hits:
        lines.append(f"• {hit.item.title} — {hit.snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commitment slice renderers (Phase 2, Slice 3). Deterministic templates only.
# ---------------------------------------------------------------------------


def _format_due_line(title: str, due_at: datetime | None, *, overdue: bool = False) -> str:
    """One reminder line. Deterministic: no LLM, no guessing."""
    if due_at is None:
        return f"• {title}"
    when = due_at.astimezone(UTC).strftime("%d/%m")
    if overdue:
        return f"• {title} — venceu em {when}"
    return f"• {title} — até {when}"


def render_commitment_reminders(notes: Sequence[CommitmentCtx | Commitment]) -> str:
    """The "📌 Lembretes" block appended to in-conversation responses.

    Accepts CommitmentCtx (from a ContextSnapshot) or Commitment (direct).
    Titles only — details never reach the chat rendering path.
    """
    lines = ["📌 Lembretes:"]
    for note in notes:
        if isinstance(note, Commitment):
            overdue = note.due_at is not None and note.due_at <= datetime.now(UTC)
            lines.append(_format_due_line(note.title, note.due_at, overdue=overdue))
        else:
            lines.append(_format_due_line(note.title, note.due_at, overdue=note.overdue))
    return "\n".join(lines)


def render_commitment_create_response(commitment: Commitment) -> str:
    """Confirmation for an explicit commitment creation."""
    if commitment.due_at is None:
        return f"Certo, vou te cobrar: {commitment.title}"
    when = commitment.due_at.astimezone(UTC).strftime("%d/%m")
    return f"Certo, vou te cobrar até {when}: {commitment.title}"


def render_commitment_fulfill_response(commitment: Commitment) -> str:
    """Confirmation for an explicit fulfillment."""
    return f"Feito! Marquei como concluído: {commitment.title}"
