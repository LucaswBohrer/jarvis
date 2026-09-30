"""Alembic async environment for JARVIS Phase 1."""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jarvis.adapters.persistence.models import Base  # noqa: E402
from jarvis.config import references_nexus_database  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

JARVIS_HEAD = "0004"


def _db_url() -> str:
    url = os.environ.get("JARVIS_DATABASE_URL", "sqlite+aiosqlite:///./data/jarvis.db")
    if references_nexus_database(url):
        raise RuntimeError("migrations must never target the NEXUS database")
    return url


def _ensure_parent_dir(url: str) -> None:
    """Create the sqlite file's parent dir on first boot (F3.4 clean-room fix).

    The app creates the parent dir at startup (dependencies.build_app_state),
    but alembic runs before the app exists -- a fresh clone has no ./data/.
    Mirrors Database.db_file(): for "sqlite+aiosqlite:///./data/jarvis.db"
    the file path is "./data/jarvis.db" (relative to CWD), NOT urlsplit's
    absolute "/data/jarvis.db". Only applies to local sqlite URLs; the NEXUS
    guard in _db_url runs first.
    """
    if "://" not in url:
        return
    # Mirror Database.db_file() exactly: strip the "scheme:///" prefix so
    # "sqlite+aiosqlite:///./data/jarvis.db" -> "./data/jarvis.db" (relative
    # to CWD) and "sqlite+aiosqlite:////tmp/x.db" -> "/tmp/x.db" (absolute).
    path = url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    else:
        return  # not a recognized local sqlite URL form
    if not path or path == ":memory:":
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    url = _db_url()
    _ensure_parent_dir(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = _db_url()
    _ensure_parent_dir(url)
    config.set_main_option("sqlalchemy.url", url)
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
