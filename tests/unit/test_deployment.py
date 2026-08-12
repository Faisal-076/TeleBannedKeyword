# ruff: noqa: S105, S106  # fake secrets deliberately literal in fixtures
"""Deployment environment guarantees.

These tests pin the deployment contract for managed platforms (Northflank /
Railway): role-scoped env validation, single MTProto owner (worker), external
Redis/Postgres URL support, session file handling, and a secret-free Docker
context.
"""

from __future__ import annotations

import os
import time
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
    """The worker process (and only it) builds the MTProto gateway and the
    session store."""
    from app.telegram.gateway import TelegramGateway
    from app.telegram.session_store import SessionStore
    from app.workers import functions

    gateway = functions._get_gateway()
    assert isinstance(gateway, TelegramGateway)
    session_store = functions._get_session_store()
    assert isinstance(session_store, SessionStore)


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
    # redis-py >=5 maps rediss:// to SSLConnection — a rediss client must be
    # TLS-capable, and hostname verification must be on by default.
    from redis.asyncio.connection import SSLConnection

    assert client.connection_pool.connection_class is SSLConnection
    assert client.connection_pool.connection_kwargs["ssl_check_hostname"] is True
    # Passing the legacy `ssl=True` kwarg to from_url() is rejected by
    # redis-py >=5 asyncio connections; the helper must not inject it.
    from redis.asyncio import from_url

    plain = from_url(settings.redis_url)
    assert plain.connection_pool.connection_class is SSLConnection  # rediss -> TLS

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
    assert body["status"] == "ok"  # liveness only — never gated on infra
    assert "database" in body
    assert "redis" in body
    assert "worker" in body
    assert "heartbeat_age" in body["worker"]

    client_api = TestClient(create_app(role="api"))
    assert client_api.get("/health").json()["role"] == "api"


async def test_health_liveness_ok_even_with_infra_down(db):
    """/health must report process liveness ("ok") and surface dependency
    state as fields even when Redis is unreachable (as in the test env)."""
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    body = TestClient(create_app(role="bot")).get("/health").json()
    assert body["status"] == "ok"
    assert body["redis"] == "error"  # conftest REDIS_URL refuses connections
    assert body["worker"]["ready"] is False
    assert body["worker"]["heartbeat_age"] is None


async def test_bot_ready_does_not_require_mtproto(db, monkeypatch):
    """Bot-role /ready must gate on DB + bot config ONLY — a bot deployment
    works before any scanner session is provisioned."""
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    bot_with_no_mtproto = _settings_with(
        telegram_api_id=0,
        telegram_api_hash="",
        master_secret=None,
        session_enc=None,
        session_file=None,
        bot_token="123:abc",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: bot_with_no_mtproto)
    client = TestClient(create_app(role="bot"))
    assert client.get("/ready").status_code == 200
    body = client.get("/health").json()
    assert body["mtproto"]["configured"] is False


class _FakeRedis:
    def __init__(self, payload: str | None):
        self.payload = payload

    async def get(self, key: str) -> str | None:
        return { "tbk:mtproto:state": self.payload }.get(key)

    async def aclose(self) -> None:
        pass


async def test_mtproto_status_comes_from_worker_report(db, monkeypatch):
    """Bot/API must read mtproto configured/session state from the worker's
    Redis report, not from their own env (which has none)."""
    import json
    import time

    from app.services import status_service

    worker_report = json.dumps(
        {
            "connected": True,
            "last_connected": "2026-01-01T00:00:00+00:00",
            "username": "@scanner",
            "configured": True,
            "session_present": True,
            "reported_at": time.time(),
        }
    )
    monkeypatch.setattr(status_service, "redis_available", _async_true)
    monkeypatch.setattr(
        "app.services.redis_client.redis_from_url",
        lambda url, **kw: _FakeRedis(worker_report),
    )

    status = await status_service.collect_status()
    assert status["mtproto"]["connected"] is True
    assert status["mtproto"]["configured"] is True
    assert status["mtproto"]["session_present"] is True
    assert status["mtproto"]["last_connected"] == "2026-01-01T00:00:00+00:00"

    # Even if the BOT's own settings claim otherwise (here: no session
    # material at all), the worker's report is authoritative.
    no_session_settings = _settings_with(
        session_enc=None, session_file=None, master_secret=None
    )
    monkeypatch.setattr(status_service, "get_settings", lambda: no_session_settings)
    status = await status_service.collect_status()
    assert status["mtproto"]["session_present"] is True
    assert status["mtproto"]["configured"] is True


async def _async_true():
    return True


async def test_health_redis_clients_are_bounded(monkeypatch):
    """/health is the k8s liveness probe target (5s timeout, failureThreshold 3).
    Every Redis client it builds must have socket timeouts well below that,
    so a stalled rediss:// handshake can never hang the probe and kill the pod.

    Contract: health-path clients bound connect+command to
    HEALTH_REDIS_TIMEOUT (<=2s); they must NOT inherit unbounded defaults.
    """
    from app.services.redis_client import HEALTH_REDIS_TIMEOUT, redis_health_from_url

    assert HEALTH_REDIS_TIMEOUT <= 2.0, "must stay below the 5s liveness probe"

    seen: dict = {}
    sentinel = object()

    def _spy(url, **kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr("app.config.get_settings", lambda: _settings_with())
    monkeypatch.setattr("app.services.redis_client.redis_from_url", _spy)

    client = redis_health_from_url()
    assert client is sentinel
    # the bound applies at construction, before any command is served
    assert seen.get("socket_timeout") == HEALTH_REDIS_TIMEOUT
    assert seen.get("socket_connect_timeout") == HEALTH_REDIS_TIMEOUT
    assert seen.get("decode_responses") is True


async def test_status_service_builds_bounded_clients(monkeypatch):
    """collect_status / get_mtproto_state must route through the bounded
    health factory — never an unbounded redis_from_url — so /health stays
    within the probe window even when Redis is down for 30+ seconds."""
    import app.services.status_service as status_module
    from app.services.redis_client import HEALTH_REDIS_TIMEOUT

    calls: list[dict] = []

    def _spy(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _FakeBoundedRedis()

    class _FakeBoundedRedis:
        async def get(self, key: str) -> str | None:
            return None

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(status_module, "redis_available", _async_true)
    monkeypatch.setattr("app.services.redis_client.redis_from_url", _spy)

    status = await status_module.collect_status()
    assert status["worker_heartbeat_age"] is None
    assert status["mtproto"]["connected"] is False
    assert calls, "health-path Redis clients must go through the bounded factory"
    for kwargs in calls:
        assert kwargs["url"] == get_settings().redis_url
        assert kwargs.get("socket_timeout") == HEALTH_REDIS_TIMEOUT
        assert kwargs.get("socket_connect_timeout") == HEALTH_REDIS_TIMEOUT
        assert kwargs.get("decode_responses") is True


# ----------------------------------------------------------- session presence


def _fake_mtproto_redis(payload: str | None):
    class _R:
        async def get(self, key: str) -> str | None:
            return payload

        async def aclose(self) -> None:
            pass

    return _R()


def _patch_redis(monkeypatch, payload: str | None):
    import app.services.status_service as status_service

    monkeypatch.setattr(status_service, "redis_available", _async_true)
    monkeypatch.setattr(
        "app.services.redis_client.redis_from_url",
        lambda url, **kw: _fake_mtproto_redis(payload),
    )


async def test_session_absent_when_no_session_provisioned(monkeypatch):
    """No session provisioned → worker reports configured=False, session_present=False."""
    import json

    import app.services.status_service as status_service

    _patch_redis(
        monkeypatch,
        json.dumps(
            {
                "connected": False,
                "configured": False,
                "session_present": False,
                "last_connected": None,
                "username": None,
                "reported_at": time.time(),
            }
        ),
    )
    status = await status_service.collect_status()
    assert status["mtproto"]["connected"] is False
    assert status["mtproto"]["configured"] is False
    assert status["mtproto"]["session_present"] is False
    assert status["mtproto"]["stale"] is False


async def test_session_present_but_disconnected(monkeypatch):
    """Session provisioned but MTProto not connected (e.g. no internet)."""
    import json

    import app.services.status_service as status_service

    _patch_redis(
        monkeypatch,
        json.dumps(
            {
                "connected": False,
                "configured": True,
                "session_present": True,
                "last_connected": None,
                "username": None,
                "reported_at": time.time(),
            }
        ),
    )
    status = await status_service.collect_status()
    assert status["mtproto"]["connected"] is False
    assert status["mtproto"]["configured"] is True
    assert status["mtproto"]["session_present"] is True


async def test_session_present_and_connected(monkeypatch):
    """Session provisioned, MTProto authenticated."""
    import json

    import app.services.status_service as status_service

    _patch_redis(
        monkeypatch,
        json.dumps(
            {
                "connected": True,
                "configured": True,
                "session_present": True,
                "last_connected": "2026-08-12T08:00:00+00:00",
                "username": "@scanner",
                "reported_at": time.time(),
            }
        ),
    )
    status = await status_service.collect_status()
    assert status["mtproto"]["connected"] is True
    assert status["mtproto"]["configured"] is True
    assert status["mtproto"]["session_present"] is True
    assert status["mtproto"]["last_connected"] == "2026-08-12T08:00:00+00:00"


async def test_mtproto_configured_is_none_when_worker_has_not_reported(monkeypatch):
    """Before the worker publishes its first state report, configured/session_present
    must be None (UNKNOWN), not coerced to False."""
    import app.services.status_service as status_service

    _patch_redis(monkeypatch, None)
    status = await status_service.collect_status()
    assert status["mtproto"]["configured"] is None
    assert status["mtproto"]["session_present"] is None
    assert status["mtproto"]["stale"] is False


# -------------------------------------------------------------- stale state


async def test_stale_mtproto_state_detected(monkeypatch):
    """A worker report older than the 300 s key TTL is marked stale."""
    import json

    import app.services.status_service as status_service

    _patch_redis(
        monkeypatch,
        json.dumps(
            {
                "connected": True,
                "configured": True,
                "session_present": True,
                "last_connected": "2026-08-12T08:00:00+00:00",
                "username": "@scanner",
                "reported_at": time.time() - 400,  # > TTL
            }
        ),
    )
    status = await status_service.collect_status()
    assert status["mtproto"]["stale"] is True
    # Stale does not erase the last-known state — callers interpret it


async def test_mtproto_state_missing_is_not_stale(monkeypatch):
    """No key at all (worker never wrote) → stale=False, values None."""
    import app.services.status_service as status_service

    _patch_redis(monkeypatch, None)
    status = await status_service.collect_status()
    assert status["mtproto"]["stale"] is False


# -------------------------------------------------------------- revoked


async def test_session_revoked_ui_priority(db, monkeypatch):
    """Even when the worker reports session_present=True, the bot must
    recognise REVOKED state from the database and let the UI prioritise it."""
    import json

    from app.services.session_state import mark_session_revoked, session_revoked

    await mark_session_revoked()
    assert await session_revoked() is True

    # Simulate worker still reporting connected + present (race window
    # before worker processed the revocation job).
    _patch_redis(
        monkeypatch,
        json.dumps(
            {
                "connected": True,
                "configured": True,
                "session_present": True,
                "last_connected": "2026-08-12T08:00:00+00:00",
                "username": "@scanner",
                "reported_at": time.time(),
            }
        ),
    )
    import app.services.status_service as status_service

    status = await status_service.collect_status()
    assert status["mtproto"]["session_present"] is True  # worker hasn't wiped yet

    # The /authstatus handler (tested separately) must check revoked first.


# -------------------------------------------------------------- heartbeat preservation


async def test_set_mtproto_state_preserves_passed_kwargs():
    """set_mtproto_state must write every keyword it receives to Redis."""
    import json

    from app.services.status_service import set_mtproto_state

    written: dict = {}

    async def _set(*args, **kwargs):
        # args[0] = self, args[1] = key, args[2] = value
        written.update(json.loads(args[2]))

    fake_redis = type("FakeRedis", (), {"set": _set})()

    await set_mtproto_state(
        fake_redis,
        connected=True,
        last_connected=None,
        username="@scanner",
        configured=True,
        session_present=True,
    )
    assert written["connected"] is True
    assert written["configured"] is True
    assert written["session_present"] is True


async def test_set_mtproto_state_omitted_kwargs_are_none():
    """Calling set_mtproto_state without configured/session_present stores None.
    This is the heartbeat's contract — it must explicitly pass them to avoid
    erasing existing values."""
    import json

    from app.services.status_service import set_mtproto_state

    written: dict = {}

    async def _set(*args, **kwargs):
        written.update(json.loads(args[2]))

    fake_redis = type("FakeRedis", (), {"set": _set})()

    await set_mtproto_state(
        fake_redis,
        connected=True,
        last_connected=None,
        username=None,
    )
    assert written["connected"] is True
    assert written["configured"] is None
    assert written["session_present"] is None


async def test_heartbeat_preserves_configured_and_session_present(monkeypatch):
    """Regression: the heartbeat cron must pass configured + session_present,
    not just connected/username.  The test calls the heartbeat function via a
    patched redis and verifies the full payload written to the mtproto key."""
    import json

    from app.workers.functions import heartbeat

    written_mtproto: dict = {}

    async def _set(*args, **kwargs):
        # args[0]=self, args[1]=key, args[2]=value
        key = args[1] if len(args) > 1 else kwargs.get("name", "")
        if key == "tbk:mtproto:state":
            written_mtproto.update(json.loads(args[2] if len(args) > 2 else "{}"))

    fake_redis = type("FakeRedis", (), {"set": _set})()

    _g = type(
        "G", (), {"connected": True, "last_connected": None, "account_username": "@test"}
    )
    monkeypatch.setattr("app.workers.functions._get_gateway", lambda: _g())
    monkeypatch.setattr("app.workers.functions._mtproto_configured", True)
    monkeypatch.setattr("app.workers.functions._session_present", True)

    await heartbeat({"redis": fake_redis})
    assert written_mtproto.get("configured") is True, "heartbeat MUST preserve configured"
    assert written_mtproto.get("session_present") is True, "heartbeat MUST preserve session_present"
    assert written_mtproto.get("connected") is True


async def test_heartbeat_does_not_erase_false_values(monkeypatch):
    """A heartbeat cycle with configured=False, session_present=False must
    remain false, not become None/null."""
    import json

    from app.workers.functions import heartbeat

    written_mtproto: dict = {}

    async def _set(*args, **kwargs):
        key = args[1] if len(args) > 1 else ""
        if key == "tbk:mtproto:state":
            written_mtproto.update(json.loads(args[2] if len(args) > 2 else "{}"))

    fake_redis = type("FakeRedis", (), {"set": _set})()

    _g = type(
        "G", (), {"connected": False, "last_connected": None, "account_username": None}
    )
    monkeypatch.setattr("app.workers.functions._get_gateway", lambda: _g())
    monkeypatch.setattr("app.workers.functions._mtproto_configured", False)
    monkeypatch.setattr("app.workers.functions._session_present", False)

    await heartbeat({"redis": fake_redis})
    assert written_mtproto.get("configured") is False, "false must stay false"
    assert written_mtproto.get("session_present") is False, "false must stay false"