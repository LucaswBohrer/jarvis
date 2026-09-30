"""Slice 3 unit tests: commitment contracts, lifecycle transitions and the
deterministic pt-BR due-date parser (test matrix T13, unit part)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from jarvis.application.commitments import parse_due_date, split_due_expression
from jarvis.domain.contracts.commitments import (
    COMMITMENT_TERMINAL_STATUSES,
    Commitment,
    CommitmentStatus,
    SweepResult,
    transition_commitment,
)


def _now() -> datetime:
    return datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # a Wednesday


# -- contract --------------------------------------------------------------


def test_status_values_match_schema_check():
    assert {s.value for s in CommitmentStatus} == {
        "open",
        "fulfilled",
        "expired",
        "cancelled",
    }
    assert "done" not in {s.value for s in CommitmentStatus}  # old 0003 value


def test_terminal_statuses():
    assert COMMITMENT_TERMINAL_STATUSES == frozenset(
        {CommitmentStatus.FULFILLED, CommitmentStatus.EXPIRED, CommitmentStatus.CANCELLED}
    )
    assert CommitmentStatus.OPEN not in COMMITMENT_TERMINAL_STATUSES


def test_commitment_is_frozen_and_forbids_extra():
    c = Commitment(title="regar as plantas")
    with pytest.raises(ValidationError):
        c.title = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Commitment(title="x", unknown_field=1)  # type: ignore[call-arg]


def test_commitment_title_and_detail_bounds():
    with pytest.raises(ValidationError):
        Commitment(title="")
    with pytest.raises(ValidationError):
        Commitment(title="x" * 201)
    with pytest.raises(ValidationError):
        Commitment(title="ok", detail="x" * 2001)
    assert Commitment(title="ok").status is CommitmentStatus.OPEN


# -- lifecycle transitions ---------------------------------------------------


def test_open_transitions():
    assert transition_commitment(CommitmentStatus.OPEN, CommitmentStatus.FULFILLED)
    assert transition_commitment(CommitmentStatus.OPEN, CommitmentStatus.EXPIRED)
    assert transition_commitment(CommitmentStatus.OPEN, CommitmentStatus.CANCELLED)


@pytest.mark.parametrize("terminal", list(COMMITMENT_TERMINAL_STATUSES))
@pytest.mark.parametrize("target", list(CommitmentStatus))
def test_terminal_states_are_immutable(terminal, target):
    with pytest.raises(ValueError):
        transition_commitment(terminal, target)


def test_open_to_open_is_rejected():
    with pytest.raises(ValueError):
        transition_commitment(CommitmentStatus.OPEN, CommitmentStatus.OPEN)


# -- SweepResult --------------------------------------------------------------


def test_sweep_result_defaults():
    r = SweepResult(ran_at=_now())
    assert r.cooldown_skipped is False
    assert r.expired_commitment_ids == []
    assert r.expired_memory_ids == []


# -- due-date parser -----------------------------------------------------------


def test_parse_due_date_keywords():
    now = _now()
    assert parse_due_date("amanhã", now) == datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
    assert parse_due_date("AMANHA", now) == datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
    assert parse_due_date("hoje", now) == datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)


def test_parse_due_date_weekdays():
    now = _now()  # Wednesday
    assert parse_due_date("sexta", now).date().isoformat() == "2026-10-02"
    assert parse_due_date("sexta-feira", now).date().isoformat() == "2026-10-02"
    # Same weekday as today resolves to today (not +7).
    assert parse_due_date("quarta", now).date().isoformat() == "2026-09-30"


def test_parse_due_date_dd_mm():
    now = _now()
    assert parse_due_date("05/10", now).date().isoformat() == "2026-10-05"
    # A date earlier in the year rolls to next year.
    assert parse_due_date("01/01", now).date().isoformat() == "2027-01-01"


def test_parse_due_date_rejects_garbage():
    now = _now()
    assert parse_due_date("quando der", now) is None
    assert parse_due_date("31/02", now) is None
    assert parse_due_date("", now) is None
    assert parse_due_date("   ", now) is None


def test_split_due_expression():
    now = _now()
    title, due = split_due_expression("revisar o relatório até amanhã", now)
    assert title == "revisar o relatório"
    assert due == datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
    # Unparsable date stays in the title, due_at=None — never guessed.
    title, due = split_due_expression("pagar a conta até quando der", now)
    assert title == "pagar a conta até quando der"
    assert due is None
    # No trailing expression at all.
    title, due = split_due_expression("regar as plantas", now)
    assert title == "regar as plantas"
    assert due is None
