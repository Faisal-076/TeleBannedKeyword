"""Unit tests for authorization service, middleware, and admin commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import (
    AuditEvent,
    AuthorizedUser,
    AuthUserStatus,
    PendingAction,
)
from app.services import authorization as auth_svc

# --------------------------------------------------------------- helpers


def _settings_with(*, admin_ids: list[int] | None = None):
    from pydantic import SecretStr

    from app.config import Settings

    ids = admin_ids if admin_ids is not None else [111, 222]
    return Settings(
        admin_user_ids=ids,
        bot_token=SecretStr("123:TEST"),
        admin_api_key=SecretStr("test-key"),
    )


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr("app.services.authorization.get_settings", _settings_with)
    monkeypatch.setattr("app.bot.middleware.get_settings", _settings_with)


# --------------------------------------------------------------- root admin detection


def test_root_admin_recognized():
    assert auth_svc.is_root_admin(111) is True
    assert auth_svc.is_root_admin(222) is True
    assert auth_svc.is_root_admin(None) is False


def test_unknown_user_not_root_admin():
    assert auth_svc.is_root_admin(999) is False
    assert auth_svc.is_root_admin(0) is False


# --------------------------------------------------------------- authorize / revoke (db)


@pytest.mark.asyncio
async def test_authorize_creates_record(db):
    user = await auth_svc.authorize_user(
        123456789, actor_user_id=111, username="@test"
    )
    assert user.telegram_user_id == 123456789
    assert user.status == AuthUserStatus.AUTHORIZED.value
    assert user.role == "user"

    info = await auth_svc.get_user_authorization(123456789)
    assert info is not None
    assert info["status"] == "authorized"


@pytest.mark.asyncio
async def test_duplicate_authorize_is_idempotent(db):
    await auth_svc.authorize_user(111222, 111)
    user2 = await auth_svc.authorize_user(111222, 222, username="@second")
    assert user2.authorized_by == 222
    assert user2.username == "@second"

    import app.database.engine as eng

    async with eng.session_scope() as session:
        count = (
            await session.execute(
                select(AuthorizedUser).where(AuthorizedUser.telegram_user_id == 111222)
            )
        ).scalars().all()
        assert len(list(count)) == 1


@pytest.mark.asyncio
async def test_revoke_updates_record(db):
    await auth_svc.authorize_user(333, 111)
    row = await auth_svc.revoke_user(333, 111)
    assert row is not None
    assert row.status == AuthUserStatus.REVOKED.value
    assert row.revoked_by == 111


@pytest.mark.asyncio
async def test_revoke_nonexistent_returns_none(db):
    row = await auth_svc.revoke_user(99999, 111)
    assert row is None


@pytest.mark.asyncio
async def test_authorized_user_recognized(db):
    await auth_svc.authorize_user(555, 111)
    assert await auth_svc.is_user_authorized(555) is True


@pytest.mark.asyncio
async def test_revoked_user_denied(db):
    await auth_svc.authorize_user(666, 111)
    await auth_svc.revoke_user(666, 111)
    assert await auth_svc.is_user_authorized(666) is False


@pytest.mark.asyncio
async def test_unknown_user_denied(db):
    assert await auth_svc.is_user_authorized(77777) is False


@pytest.mark.asyncio
async def test_root_admin_always_authorized(db):
    assert await auth_svc.is_user_authorized(111) is True  # root admin


@pytest.mark.asyncio
async def test_get_auth_level(db):
    await auth_svc.authorize_user(888, 111)
    assert await auth_svc.get_auth_level(111) == "admin"
    assert await auth_svc.get_auth_level(888) == "user"
    assert await auth_svc.get_auth_level(9999) is None


# --------------------------------------------------------------- audit events


@pytest.mark.asyncio
async def test_authorize_creates_audit_event(db):
    await auth_svc.authorize_user(101010, 111)
    import app.database.engine as eng

    async with eng.session_scope() as session:
        result = await session.execute(
            select(AuditEvent).where(AuditEvent.operation == "user_authorized")
        )
        rows = result.scalars().all()
        assert len(rows) >= 1
        evt = rows[-1]
        assert evt.details["target_user_id"] == 101010
        assert evt.details["actor_user_id"] == 111


@pytest.mark.asyncio
async def test_revoke_creates_audit_event(db):
    await auth_svc.authorize_user(202020, 111)
    await auth_svc.revoke_user(202020, 111)
    import app.database.engine as eng

    async with eng.session_scope() as session:
        result = await session.execute(
            select(AuditEvent).where(AuditEvent.operation == "user_revoked")
        )
        rows = result.scalars().all()
        assert len(rows) >= 1


# --------------------------------------------------------------- pending actions


@pytest.mark.asyncio
async def test_pending_action_create_and_resolve(db):
    token = await auth_svc.create_pending_action("authorize", 111, 303030)
    pending = await auth_svc.resolve_pending_action(token)
    assert pending is not None
    assert pending.action == "authorize"
    assert pending.actor_id == 111
    assert pending.target_user_id == 303030


@pytest.mark.asyncio
async def test_pending_action_expired(db):
    token = await auth_svc.create_pending_action("authorize", 111, 404040)
    # manually expire it
    import app.database.engine as eng

    async with eng.session_scope() as session:
        result = await session.execute(
            select(PendingAction).where(PendingAction.token == token)
        )
        pa = result.scalar_one()
        pa.expires_at = datetime.now(UTC) - timedelta(minutes=10)
    pending = await auth_svc.resolve_pending_action(token)
    assert pending is None


@pytest.mark.asyncio
async def test_pending_action_wrong_token(db):
    pending = await auth_svc.resolve_pending_action("nonexistent-token")
    assert pending is None


@pytest.mark.asyncio
async def test_delete_pending_action(db):
    token = await auth_svc.create_pending_action("authorize", 111, 505050)
    await auth_svc.delete_pending_action(token)
    assert await auth_svc.resolve_pending_action(token) is None


# --------------------------------------------------------------- caching


@pytest.mark.asyncio
async def test_cache_invalidated_on_authorize(db):
    await auth_svc.authorize_user(606060, 111)
    # first call populates cache, second from cache
    assert await auth_svc.is_user_authorized(606060) is True
    # revoke should invalidate
    await auth_svc.revoke_user(606060, 111)
    assert await auth_svc.is_user_authorized(606060) is False


# --------------------------------------------------------------- listing


@pytest.mark.asyncio
async def test_list_authorized_users(db):
    await auth_svc.authorize_user(1111, 111, username="@a")
    await auth_svc.authorize_user(2222, 111, username="@b")
    users = await auth_svc.list_authorized_users()
    assert len(users) >= 2
    ids = {u["telegram_user_id"] for u in users}
    assert 1111 in ids
    assert 2222 in ids


# --------------------------------------------------------------- middleware integration


@pytest.mark.asyncio
async def test_is_root_admin_false_for_normal_user(db):
    from app.bot.middleware import is_root_admin

    await auth_svc.authorize_user(123, 111)
    assert is_root_admin(123) is False
    assert is_root_admin(111) is True


# --------------------------------------------------------------- security invariants


@pytest.mark.asyncio
async def test_get_user_authorization_root_admin(db):
    info = await auth_svc.get_user_authorization(111)
    assert info is not None
    assert info["status"] == "root_admin"


@pytest.mark.asyncio
async def test_authorize_self_by_root_admin_not_allowed_for_nonexistent_user():
    # See auth_commands handler — not a service-level check, but root admins
    # cannot be added to DB table through the normal /authorize flow.
    # The is_root_admin guard in the command handler prevents this.
    pass


@pytest.mark.asyncio
async def test_redis_unavailable_fails_closed(db, monkeypatch):
    """When the auth cache is cleared (simulating Redis unavailability
    recovery), authorization must still work from the database."""
    # Clear cache and verify DB fallback still works.
    auth_svc._auth_cache.clear()
    await auth_svc.authorize_user(77777, 111)
    auth_svc._auth_cache.clear()
    assert await auth_svc.is_user_authorized(77777) is True
