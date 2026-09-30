"""F3.4.1 regression: async DB lifecycle.

``Database.close()`` disposes the engine (closing pooled aiosqlite
connections *before* the event loop ends). It must be safe to call twice:
several tests close ``repos["db"]`` themselves while the ``repos`` fixture
now also closes it in teardown.
"""

from __future__ import annotations

from jarvis.adapters.persistence.database import Database


async def test_close_is_idempotent(db_url: str) -> None:
    db = Database(db_url)
    assert await db.schema_ok("0004")
    await db.close()
    await db.close()  # second close must not raise


async def test_engine_usable_until_close(db_url: str) -> None:
    db = Database(db_url)
    async with db.session() as s:
        assert s is not None
    await db.close()
