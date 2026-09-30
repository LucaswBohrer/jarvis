"""Async SQLite database: engine, pragmas, session factory, schema checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jarvis.config import references_nexus_database

BUSY_TIMEOUT_MS = 5000


def _apply_pragmas(dbapi_conn: object, _record: object) -> None:
    cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


class Database:
    """Owns the JARVIS-local SQLite file. Never points at the NEXUS database."""

    def __init__(self, url: str) -> None:
        if not url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("JARVIS database must be a local sqlite+aiosqlite URL")
        if references_nexus_database(url):
            raise ValueError("JARVIS database URL must never reference NEXUS")
        self._url = url
        self._engine: AsyncEngine = create_async_engine(url, future=True)
        event.listen(self._engine.sync_engine, "connect", _apply_pragmas)
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def url(self) -> str:
        return self._url

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            yield session

    async def schema_version(self) -> str | None:
        """Current alembic version, or None when migrations never ran."""
        async with self.session() as session:
            exists = await session.run_sync(
                lambda sync_session: sa.inspect(sync_session.get_bind()).has_table(
                    "alembic_version"
                )
            )
            if not exists:
                return None
            row = (
                await session.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).first()
            return row[0] if row else None

    async def schema_ok(self, expected_version: str) -> bool:
        return await self.schema_version() == expected_version

    def db_file(self) -> Path | None:
        path = self._url.removeprefix("sqlite+aiosqlite:///")
        return Path(path) if path != ":memory:" else None

    async def close(self) -> None:
        await self._engine.dispose()
