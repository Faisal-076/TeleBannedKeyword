"""Authorization service: manage authorized bot users.

Root admins (``ADMIN_USER_IDS``) are always authoritative and never need a
DB record.  Normal users are stored in ``authorized_users`` with an explicit
status/role.

Every mutation produces an audit event via the existing ``AuditEvent`` table.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import (
    AuditEvent,
    AuthorizedUser,
    AuthUserRole,
    AuthUserStatus,
    PendingAction,
)

logger = logging.getLogger("app.services.auth")

ACTION_TTL_MINUTES = 5
CLEANUP_INTERVAL = 60

_auth_cache: dict[int, str | None] = {}
_cleanup_at = 0.0


def is_root_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in set(get_settings().admin_user_ids)


async def is_user_authorized(user_id: int | None) -> bool:
    """True if user is a root admin OR has an active AUTHORIZED DB record."""
    if is_root_admin(user_id):
        return True
    if user_id is None:
        return False
    result = await _get_cached_auth(user_id)
    return result == AuthUserStatus.AUTHORIZED.value


async def get_auth_level(user_id: int | None) -> str | None:
    """Return 'admin', 'user', or None (unauthorized)."""
    if is_root_admin(user_id):
        return "admin"
    if user_id is None:
        return None
    result = await _get_cached_auth(user_id)
    if result == AuthUserStatus.AUTHORIZED.value:
        return "user"
    return None


async def _get_cached_auth(user_id: int) -> str | None:
    global _cleanup_at

    import time

    now = time.monotonic()
    if now - _cleanup_at > CLEANUP_INTERVAL:
        _auth_cache.clear()
        _cleanup_at = now

    key = f"auth:{user_id}"
    cached = _auth_cache.get(key)
    if cached is not None:
        return cached

    async with session_scope() as session:
        row = await session.execute(
            select(AuthorizedUser.status).where(
                AuthorizedUser.telegram_user_id == user_id
            )
        )
        status = row.scalar()
    _auth_cache[key] = status
    return status


def _invalidate_cache(user_id: int) -> None:
    _auth_cache.pop(f"auth:{user_id}", None)


async def authorize_user(
    target_user_id: int,
    actor_user_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
) -> AuthorizedUser:
    """Authorize (or re-authorize) a user.  Idempotent on telegram_user_id."""
    async with session_scope() as session:
        stmt = (
            pg_insert(AuthorizedUser)
            .values(
                telegram_user_id=target_user_id,
                username=username,
                display_name=display_name,
                status=AuthUserStatus.AUTHORIZED.value,
                role=AuthUserRole.USER.value,
                authorized_by=actor_user_id,
                authorized_at=datetime.datetime.now(datetime.UTC),
                notes=notes,
            )
            .on_conflict_do_update(
                index_elements=["telegram_user_id"],
                set_={
                    "status": AuthUserStatus.AUTHORIZED.value,
                    "username": username,
                    "display_name": display_name,
                    "authorized_by": actor_user_id,
                    "authorized_at": datetime.datetime.now(datetime.UTC),
                    "revoked_by": None,
                    "revoked_at": None,
                    "notes": notes,
                },
            )
        )
        await session.execute(stmt)
        result = await session.execute(
            select(AuthorizedUser).where(AuthorizedUser.telegram_user_id == target_user_id)
        )
        user = result.scalar_one()
        _record_audit(session, "user_authorized", target_user_id, actor_user_id)

    _invalidate_cache(target_user_id)
    return user


async def revoke_user(target_user_id: int, actor_user_id: int) -> AuthorizedUser | None:
    """Revoke an authorized user.  Returns None if the user was not in the table."""
    async with session_scope() as session:
        result = await session.execute(
            select(AuthorizedUser).where(
                AuthorizedUser.telegram_user_id == target_user_id,
                AuthorizedUser.status == AuthUserStatus.AUTHORIZED.value,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.status = AuthUserStatus.REVOKED.value
        row.revoked_by = actor_user_id
        row.revoked_at = datetime.datetime.now(datetime.UTC)
        _record_audit(session, "user_revoked", target_user_id, actor_user_id)

    _invalidate_cache(target_user_id)
    return row


async def get_user_authorization(user_id: int) -> dict | None:
    """Return authorization details for an admin UI, or None."""
    if is_root_admin(user_id):
        return {
            "telegram_user_id": user_id,
            "status": "root_admin",
            "role": "admin",
            "authorized_by": None,
            "authorized_at": None,
            "username": None,
            "notes": None,
        }
    async with session_scope() as session:
        result = await session.execute(
            select(AuthorizedUser).where(AuthorizedUser.telegram_user_id == user_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return {
            "telegram_user_id": row.telegram_user_id,
            "username": row.username,
            "display_name": row.display_name,
            "status": row.status,
            "role": row.role,
            "authorized_by": row.authorized_by,
            "authorized_at": row.authorized_at.isoformat() if row.authorized_at else None,
            "revoked_by": row.revoked_by,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "notes": row.notes,
        }


async def list_authorized_users(limit: int = 100, offset: int = 0) -> list[dict]:
    """Paginated list of DB-authorized users."""
    async with session_scope() as session:
        result = await session.execute(
            select(AuthorizedUser)
            .order_by(AuthorizedUser.authorized_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = result.scalars().all()
        return [
            {
                "telegram_user_id": r.telegram_user_id,
                "username": r.username,
                "display_name": r.display_name,
                "status": r.status,
                "role": r.role,
                "authorized_at": r.authorized_at.isoformat() if r.authorized_at else None,
            }
            for r in rows
        ]


async def get_auth_audit_log(limit: int = 50, offset: int = 0) -> list[dict]:
    """Recent authorization-related audit events."""
    async with session_scope() as session:
        result = await session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.operation.in_(
                    ["user_authorized", "user_revoked", "auth_check_denied"]
                )
            )
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = result.scalars().all()
        return [
            {
                "operation": r.operation,
                "details": r.details,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


async def create_pending_action(
    action: str,
    actor_id: int,
    target_user_id: int,
    *,
    target_username: str | None = None,
) -> str:
    """Create a time-limited pending action for callback confirmation.
    Returns the token to embed in callback_data."""
    token = uuid.uuid4().hex
    expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        minutes=ACTION_TTL_MINUTES
    )
    async with session_scope() as session:
        session.add(
            PendingAction(
                token=token,
                action=action,
                actor_id=actor_id,
                target_user_id=target_user_id,
                target_username=target_username,
                expires_at=expires_at,
            )
        )
        # Clean up old rows
        await session.execute(
            select(PendingAction).where(PendingAction.expires_at < expires_at)
        )
    return token


async def resolve_pending_action(token: str) -> PendingAction | None:
    """Look up a pending action by token; returns None if expired or missing."""
    async with session_scope() as session:
        result = await session.execute(
            select(PendingAction).where(PendingAction.token == token)
        )
        action = result.scalar_one_or_none()
        if action is None:
            return None
        now = datetime.datetime.now(datetime.UTC)
        if action.expires_at.replace(tzinfo=None) < now.replace(tzinfo=None):
            # expired
            return None
        return action


async def delete_pending_action(token: str) -> None:
    async with session_scope() as session:
        result = await session.execute(
            select(PendingAction).where(PendingAction.token == token)
        )
        action = result.scalar_one_or_none()
        if action is not None:
            await session.delete(action)


def _record_audit(session, operation: str, target_user_id: int, actor_user_id: int) -> None:
    session.add(
        AuditEvent(
            operation=operation,
            status="ok",
            details={
                "target_user_id": target_user_id,
                "actor_user_id": actor_user_id,
            },
        )
    )
