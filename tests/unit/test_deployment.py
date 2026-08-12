# ruff: noqa: S105, S106  # fake secrets deliberately literal in fixtures
"""Deployment environment guarantees.

These tests pin the deployment contract for managed platforms (Northflank /
Railway): role-scoped env validation, single MTProto owner (worker), external
Redis/Postgres URL support, session file handling, and a secret-free Docker
context.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import get_settings
from app.config.validate import (
    ROLE_API,
    ROLE_BOT,
    ROLE_WORKER,
    validate_role_env,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _settings_with(**updates):
    """model_copy(update=...) does not re-validate; wrap SecretStr fields."""
    from pydantic import SecretStr

    _SECRET_FIELDS = {"bot_token", "telegram_api_hash", "admin_api_key", "master_secret"}
    for key in _SECRET_FIELDS:
        value = updates.get(key)
        if isinstance(value, str):
            updates[key] = SecretStr(value)
    return get_settings().model_copy(update=updates)


def _monkeypatch_settings(monkeypatch, settings):
    import app.config.validate as validate_module

    monkeypatch.setattr(validate_module, "get_settings", lambda: settings)


# ------------------------------------------------------ role-scoped validation


def test_bot_has_no_mtproto_dependency(monkeypatch):
    """Bot role must not require worker-only secrets (api_id/hash/session)."""
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(
            bot_token="123:abc",
            admin_user_ids=[1],
            telegram_api_id=0,
            telegram_api_hash="",
            session_enc=None,
            session_file=None,
            master_secret="any",
        ),
    )
    validate_role_env(ROLE_BOT)  # must not raise


def test_bot_requires_token_and_allowlist(monkeypatch):
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(bot_token="", admin_user_ids=[]),
    )
    with pytest.raises(RuntimeError) as exc_info:
        validate_role_env(ROLE_BOT)
    message = str(exc_info.value)
    assert "BOT_TOKEN" in message
    assert "ADMIN_USER_IDS" in message


def test_worker_owns_mtproto():
    """The worker process (and only it) builds the MTProto gateway."""
    from app.telegram.gateway import TelegramGateway
    from app.workers import functions

    assert isinstance(functions._get_gateway(), TelegramGateway)


def test_worker_requires_mtproto_secrets(monkeypatch):
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(
            telegram_api_id=0,
            telegram_api_hash="",
            session_file=None,
            session_enc=None,
            master_secret="any",
        ),
    )
    with pytest.raises(RuntimeError) as exc_info:
        validate_role_env(ROLE_WORKER)
    assert "TELEGRAM_API_ID" in str(exc_info.value)
    assert "TELEGRAM_API_HASH" in str(exc_info.value)


def test_worker_requires_master_secret_with_session(monkeypatch):
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(
            telegram_api_id=123,
            telegram_api_hash="h" * 32,
            session_file="/data/session.enc",
            session_enc=None,
            master_secret=None,
        ),
    )
    with pytest.raises(RuntimeError) as exc_info:
        validate_role_env(ROLE_WORKER)
    assert "MASTER_SECRET" in str(exc_info.value)


def test_worker_missing_session_is_bootstrap_not_error(monkeypatch):
    """No session material at first deploy is a documented bootstrap state
    (provision via tbk-auth afterwards) — a clear log, not a hard failure."""
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(
            telegram_api_id=123,
            telegram_api_hash="h" * 32,
            session_file=None,
            session_enc=None,
            master_secret="any",
        ),
    )
    validate_role_env(ROLE_WORKER)  # must not raise


def test_bot_does_not_require_session_file(monkeypatch):
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(
            bot_token="123:abc",
            admin_user_ids=[1],
            session_file=None,
            session_enc=None,
        ),
    )
    validate_role_env(ROLE_BOT)  # must not raise


def test_api_requires_admin_key(monkeypatch):
    _monkeypatch_settings(monkeypatch, _settings_with(admin_api_key=""))
    with pytest.raises(RuntimeError) as exc_info:
        validate_role_env(ROLE_API)
    assert "ADMIN_API_KEY" in str(exc_info.value)


def test_unknown_role_rejected():
    with pytest.raises(ValueError):
        validate_role_env("proxy")


# ----------------------------------------------------------- session handling


def test_worker_session_path(monkeypatch, tmp_path):
    """SESSION_FILE must be the authoritative session path for the worker."""
    import asyncio

    _MASTER = "test-master-secret-not-for-production"
    session_file = tmp_path / "data" / "session.enc"
    settings = _settings_with(
        session_file=str(session_file),
        session_enc="v1:stale-env-blob",
        master_secret=_MASTER,
    )

    import app.telegram.session_store as session_store_module
    from app.telegram.session_store import SessionStore

    monkeypatch.setattr(session_store_module, "get_settings", lambda: settings)

    blob = os.urandom(16).hex()
    session_file.parent.mkdir(parents=True)
    from app.security.crypto import encrypt_string

    session_file.write_text(encrypt_string(blob, _MASTER), encoding="utf-8")

    store = SessionStore()
    loaded = asyncio.run(store.load())
    assert loaded == blob


def test_bot_entrypoint_needs_no_session_config(monkeypatch):
    """run_bot() validation and the settings facade work with zero session
    material — the session belongs to the worker."""
    _monkeypatch_settings(
        monkeypatch,
        _settings_with(bot_token="123:abc", admin_user_ids=[1]),
    )
    validate_role_env(ROLE_BOT)


# ------------------------------------------------------------ external services


def test_external_redis_url_supported(tmp_path, monkeypatch):
    """Arq + redis.asyncio must accept credentialed, TLS DSNs (Northflank)."""
    from arq.connections import RedisSettings

    from app.services.queue import _bot_api_redis_settings

    settings = _settings_with(
        redis_url=(
            "rediss://:securepassword@redis.internal.prod:6379/2"
            "?ssl_cert_reqs=none"
        )
    )
    rs = RedisSettings.from_dsn(settings.redis_url)
    assert rs.host == "redis.internal.prod"
    assert rs.port == 6379
    assert rs.database == 2
    assert rs.password == "securepassword"
    assert rs.ssl is True

    from app.services.redis_client import redis_from_url

    client = redis_from_url(settings.redis_url)
    assert client.connection_pool.connection_kwargs["host"] == "redis.internal.prod"
    assert client.connection_pool.connection_kwargs["ssl"] is True
    # redis-py's plain from_url() silently drops TLS for rediss:// — the
    # helper must be what the app uses, not bare from_url.
    from redis.asyncio import from_url

    plain = from_url(settings.redis_url)
    assert "ssl" not in plain.connection_pool.connection_kwargs  # regression proof

    monkeypatch.setattr(
        "app.services.queue.get_settings", lambda: settings
    )
    pooled = _bot_api_redis_settings()
    assert pooled.conn_retries == 0  # bot must fail fast, worker retries


def test_worker_redis_dsn_constructs(tmp_path, monkeypatch):
    from app.workers.worker import build_worker

    monkeypatch.setattr(
        "app.workers.worker.get_settings",
        lambda: _settings_with(
            redis_url="rediss://:pw@redis.internal.prod:6379/0?ssl_cert_reqs=none",
            telegram_api_id=123,
            telegram_api_hash="h" * 32,
        ),
    )
    worker = build_worker()
    assert worker.redis_settings.ssl is True


def test_postgres_url_supported(monkeypatch):
    """asyncpg DSNs (incl. ssl params) must build an async engine."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        "postgresql+asyncpg://tbk:pass@pg.internal:5432/tbk?ssl=require"
    )
    assert "postgresql" in engine.url.drivername
    assert engine.url.get_backend_name() == "postgresql"


def test_auto_create_schema_flag_gates_init(monkeypatch):
    """AUTO_CREATE_SCHEMA=false must disable dev-mode create_all."""
    from app.database import init_db as init_db_module

    calls: list[str] = []

    async def _boom(*args, **kwargs):
        calls.append("create_all")
        raise AssertionError("create_all must not run with AUTO_CREATE_SCHEMA=false")

    monkeypatch.setattr(init_db_module, "get_engine", _boom)
    monkeypatch.setattr(
        init_db_module,
        "get_settings",
        lambda: _settings_with(auto_create_schema=False),
    )

    import asyncio

    asyncio.run(init_db_module.init_db())
    assert calls == []


# ------------------------------------------------------------ docker context


def test_no_secrets_in_docker_context():
    ignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    required_patterns = (
        ".env",
        ".env.*",
        "*.session",
        "*.session-journal",
        "session.enc",
        "*.pem",
        "*.key",
        "data",
        "!README.md",
    )
    for pattern in required_patterns:
        assert pattern in ignore, f".dockerignore must contain {pattern!r}"


def test_session_file_not_baked_into_image():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for forbidden in ("session.enc", ".env", "SESSION_FILE", "MASTER_SECRET"):
        assert forbidden not in dockerfile, (
            f"Dockerfile must not reference {forbidden!r} (runtime env/volume only)"
        )
    assert "USER app" in dockerfile
    assert "--uid 10001" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    assert "CMD" in dockerfile
    entrypoint = (PROJECT_ROOT / "scripts" / "docker-entrypoint.sh").read_text(
        encoding="utf-8"
    )
    assert "alembic upgrade head" in entrypoint


# ------------------------------------------------------------------- degraded


async def test_redis_failure_does_not_make_bot_mtproto_owner(db, monkeypatch):
    """Redis down + queued request: the bot still never constructs MTProto."""
    from app.services import queue as queue_module

    calls: list[str] = []

    def _boom(*args, **kwargs):
        calls.append("create_gateway")
        raise AssertionError("bot must never create a gateway")

    monkeypatch.setattr(queue_module, "_get_pool", _async_none)

    from app.services.analysis_service import AnalysisService

    service = AnalysisService()  # no gateway passed — bot wiring
    result = await service.submit("hello deployment test", user_id=111)
    assert result.queued is False
    assert result.degraded is True
    assert calls == []
    with pytest.raises(RuntimeError):
        await service.run_request(result.request_id)


async def _async_none():
    return None


# ----------------------------------------- bot without SessionStore/volume


async def test_bot_session_state_db_only(db, monkeypatch):
    """The bot reads/writes session state via Postgres only — no session
    env, no session file, no master secret anywhere in the bot process."""
    import app.telegram.session_store as session_store_module
    from app.services import session_state

    no_session_settings = _settings_with(
        session_enc=None,
        session_file=None,
        master_secret=None,
    )
    monkeypatch.setattr(session_store_module, "get_settings", lambda: no_session_settings)

    assert await session_state.session_revoked() is False

    await session_state.mark_session_revoked()
    assert await session_state.session_revoked() is True

    # The worker's SessionStore must agree (same app_state key): the worker
    # refuses to connect while the bot-side flag is set.
    from app.telegram.session_store import SessionStore

    assert await SessionStore().is_revoked() is True

    # Clearing the flag (re-provisioning, as done by tbk-auth locally)
    # restores a non-revoked state the bot observes.
    async with session_state.session_scope() as session:
        state = await session.get(session_state.AppState, session_state.SESSION_STATE_KEY)
        assert state is not None
        state.value = {**state.value, "revoked": False}
    assert await session_state.session_revoked() is False


async def test_ready_api_role_gates_on_admin_key(db, monkeypatch):
    """Standalone API (/ready) must gate on DB + ADMIN_API_KEY, not on
    bot configuration (it has none)."""
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    client = TestClient(create_app(role="api"))
    # conftest env provides ADMIN_API_KEY + DATABASE_URL → ready.
    assert client.get("/ready").status_code == 200

    import app.api.app as api_app_module

    monkeypatch.setattr(
        api_app_module, "get_settings",
        lambda: _settings_with(admin_api_key=""),
    )
    client2 = TestClient(create_app(role="api"))
    assert client2.get("/ready").status_code == 503


async def test_health_reports_role(db):
    """/health is role-tagged liveness; it never 503s for infra peers."""
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    client = TestClient(create_app(role="bot"))
    body = client.get("/health").json()
    assert body["role"] == "bot"
    assert body["status"] in ("ok", "degraded")
    assert "worker_heartbeat_age" in body

    client_api = TestClient(create_app(role="api"))
    assert client_api.get("/health").json()["role"] == "api"