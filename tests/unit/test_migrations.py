"""Regression tests for the migration runner.

Production failure: the advisory-lock SELECT in ``migrations/env.py`` auto-begins
a transaction on the connection. Alembic then reused that dangling transaction,
it was never committed, and ``engine.dispose()`` rolled it back -- so alembic
logged "Running upgrade" but no tables ever appeared. The worker crash-looped on
the missing ``app_state`` table (UndefinedTableError, exit code 1).

Note: SQLite cannot reproduce the rollback (its DBAPI commits pending
transactions on close, unlike asyncpg which rolls back), so the regression
guard is a contract test on the real ``run_migrations_online`` with a recording
engine, plus an end-to-end upgrade on SQLite.

``migrations.env`` executes migrations at import time, and its module body
touches alembic proxies that only exist under the alembic CLI, so the tests
patch those proxies before importing.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from alembic import command, context
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _patch_context_proxies(monkeypatch) -> None:
    """env.py reads `alembic.context.config` at import time (normally set by the
    alembic CLI) and consults `is_offline_mode()`; provide stand-ins so the
    module imports outside a CLI run."""
    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(config_file_name=None),
        raising=False,
    )
    monkeypatch.setattr(context, "is_offline_mode", lambda: False, raising=False)


def test_run_migrations_online_creates_schema(tmp_path, monkeypatch) -> None:
    """End-to-end: `alembic upgrade head` (exactly what the entrypoint runs)
    creates the full schema and the alembic version row."""
    db_path = tmp_path / "mig.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    from app.config import get_settings

    get_settings.cache_clear()
    try:
        cfg = Config()
        cfg.set_main_option("script_location", "migrations")
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()

    async def _verify() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                    )
                ).all()
            return {r[0] for r in rows}
        finally:
            await engine.dispose()

    names = asyncio.run(_verify())
    assert "alembic_version" in names
    assert "app_state" in names
    assert "analysis_requests" in names
    assert "telegram_messages" in names


def test_advisory_lock_transaction_is_committed_before_and_after_migration(monkeypatch) -> None:
    """The fix: commit() after each advisory-lock SELECT so alembic owns its own
    migration transaction instead of having the whole migration rolled back on
    dispose."""
    _patch_context_proxies(monkeypatch)
    # The module body would run the real migration at import; suppress it.
    monkeypatch.setattr(asyncio, "run", lambda _coro: None, raising=False)
    env = importlib.import_module("migrations.env")

    calls: list[str] = []

    class FakeConn:
        @property
        def dialect(self) -> SimpleNamespace:
            return SimpleNamespace(name="postgresql")

        async def execute(self, *args, **kwargs) -> None:
            calls.append("execute")

        async def commit(self) -> None:
            calls.append("commit")

        async def run_sync(self, _fn) -> None:
            calls.append("run_sync")

        async def __aenter__(self) -> FakeConn:
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

    class FakeEngine:
        def connect(self) -> FakeConn:
            return FakeConn()

        async def dispose(self) -> None:
            calls.append("dispose")

    monkeypatch.setattr(env, "create_async_engine", lambda _url: FakeEngine())

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(env.run_migrations_online())
    finally:
        loop.close()

    # lock SELECT -> commit -> alembic migration -> unlock SELECT -> commit -> dispose
    assert calls == ["execute", "commit", "run_sync", "execute", "commit", "dispose"]
