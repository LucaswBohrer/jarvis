"""Persistence: migration up/down/up round-trip.

Alembic must migrate a fresh DB to head, downgrade cleanly step by step,
and migrate back to head with identical schema version. Runs sync (alembic
manages its own event loop internally).
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.conftest import REPO


def _cfg(db_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("JARVIS_DATABASE_URL", url)
    cfg = Config(str(REPO / "alembic.ini"))
    return cfg, url


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_up_down_up(tmp_path, monkeypatch):
    db_path = tmp_path / "mig.db"
    cfg, url = _cfg(db_path, monkeypatch)

    command.upgrade(cfg, "head")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0004"]

    con = sqlite3.connect(db_path)
    assert {"sessions", "messages", "tasks", "audit_logs"} <= _tables(con)
    assert {"memory_items", "commitments", "service_meta", "memory_fts"} <= _tables(con)

    # 0004 -> 0003: FTS virtual table dropped, memory tables kept
    command.downgrade(cfg, "0003")
    assert "memory_fts" not in _tables(con)
    assert {"memory_items", "commitments", "service_meta"} <= _tables(con)

    # 0003 -> 0002: memory tables dropped, Phase 1 tables kept
    command.downgrade(cfg, "0002")
    tables = _tables(con)
    assert not ({"memory_items", "commitments", "service_meta"} & tables)
    assert {"sessions", "messages", "tasks", "audit_logs"} <= tables

    command.upgrade(cfg, "head")
    assert {"memory_items", "commitments", "service_meta", "memory_fts"} <= _tables(con)
    # append-only triggers restored
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert triggers, "expected append-only triggers after re-upgrade"
    con.close()
