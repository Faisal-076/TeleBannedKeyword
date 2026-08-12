"""Alembic async environment. Reads DATABASE_URL from application settings.

On Postgres, migrations run under a session-level advisory lock so that
multiple replicas starting concurrently cannot race `alembic upgrade head`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.database.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_MIGRATION_LOCK_KEY = "tbk:migrations"


def run_migrations_offline() -> None:
    url = get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            is_postgres = connection.dialect.name == "postgresql"
            if is_postgres:
                await connection.execute(
                    text("SELECT pg_advisory_lock(hashtext(:k))"),
                    {"k": _MIGRATION_LOCK_KEY},
                )
                # The lock SELECT auto-begins a transaction on this connection.
                # Commit it so alembic owns a fresh migration transaction below;
                # otherwise alembic reuses the dangling transaction and the
                # whole migration is rolled back when the engine disposes.
                await connection.commit()
            try:
                await connection.run_sync(do_run_migrations)
            finally:
                if is_postgres:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:k))"),
                        {"k": _MIGRATION_LOCK_KEY},
                    )
                    await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
