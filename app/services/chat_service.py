"""Target chat management, user-scoped via user_chat_targets.

MTProto resolution happens ONLY in the worker process.  Every user sees
ONLY their own configured chats.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.engine import session_scope
from app.database.models import (
    AccessMode,
    AuditEvent,
    TargetChat,
    TelegramAccessIdentity,
    UserChatTarget,
)
from app.security.redact import mask_username
from app.telegram.errors import AccessState
from app.telegram.gateway import ResolvedChat
from app.utils.timeutil import utc_now_naive

logger = logging.getLogger("app.services.chats")


@dataclass
class AddChatResult:
    ok: bool
    chat: TargetChat | None = None
    target: UserChatTarget | None = None
    error: str | None = None
    access_state: str | None = None


class ChatService:
    """Database-only chat management; never touches MTProto."""

    async def persist_resolved(
        self, resolved: ResolvedChat, actor: str, *, owner_user_id: int
    ) -> AddChatResult:
        if resolved.access_state != AccessState.ACCESSIBLE:
            return AddChatResult(ok=False, error=resolved.error or "unreachable", access_state=resolved.error)

        async with session_scope() as session:
            chat = await session.execute(
                select(TargetChat).where(TargetChat.telegram_chat_id == resolved.chat_id)
            )
            chat = chat.scalar_one_or_none()
            if chat is None:
                chat = TargetChat(telegram_chat_id=resolved.chat_id, title=resolved.title,
                                  username=resolved.username if resolved.username else None,
                                  chat_type=resolved.chat_type, access_state=AccessState.ACCESSIBLE.value,
                                  last_verified_at=utc_now_naive())
                session.add(chat)
                await session.flush()
            else:
                chat.title = resolved.title
                chat.username = resolved.username
                chat.chat_type = resolved.chat_type
                chat.access_state = AccessState.ACCESSIBLE.value
                chat.access_error = None
                chat.last_verified_at = utc_now_naive()

            stmt = pg_insert(UserChatTarget).values(
                user_id=owner_user_id, telegram_chat_id=resolved.chat_id,
                enabled=True, access_mode=AccessMode.CENTRAL_PUBLIC.value,
            ).on_conflict_do_update(
                index_elements=["user_id", "telegram_chat_id"],
                set_={"enabled": True, "access_mode": AccessMode.CENTRAL_PUBLIC.value},
            )
            await session.execute(stmt)

            session.add(AuditEvent(
                user_id_hash=actor, operation="chat_add", status="ok",
                details={"chat_id": resolved.chat_id, "owner_user_id": owner_user_id},
            ))
            return AddChatResult(ok=True, chat=chat)

    async def remove_chat(self, chat_id: int, *, user_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                select(UserChatTarget).where(
                    UserChatTarget.user_id == user_id, UserChatTarget.telegram_chat_id == chat_id)
            )
            target = result.scalar_one_or_none()
            if target is None:
                return False
            await session.delete(target)
            session.add(AuditEvent(
                operation="chat_remove", status="ok", details={"chat_id": chat_id, "user_id": user_id}))
            return True

    async def list_chats(self, user_id: int) -> list[TargetChat]:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).join(
                    UserChatTarget, TargetChat.telegram_chat_id == UserChatTarget.telegram_chat_id
                ).where(UserChatTarget.user_id == user_id).order_by(UserChatTarget.id)
            )
            return list(result.scalars().all())

    async def admin_list_chats(self) -> list[TargetChat]:
        """Root-admin global view — returns all chats regardless of owner."""
        async with session_scope() as session:
            result = await session.execute(select(TargetChat).order_by(TargetChat.id))
            return list(result.scalars().all())

    async def admin_remove_chat(self, chat_id: int) -> bool:
        """Root-admin remove — deletes the TargetChat + all UserChatTarget rows."""
        async with session_scope() as session:
            from sqlalchemy import delete
            await session.execute(
                delete(UserChatTarget).where(UserChatTarget.telegram_chat_id == chat_id)
            )
            chat = await session.execute(select(TargetChat).where(TargetChat.telegram_chat_id == chat_id))
            chat = chat.scalar_one_or_none()
            if chat is None:
                return False
            await session.delete(chat)
            return True
        async with session_scope() as session:
            result = await session.execute(
                select(UserChatTarget).where(UserChatTarget.user_id == user_id).order_by(UserChatTarget.id)
            )
            return list(result.scalars().all())

    async def get_user_target(self, user_id: int, chat_id: int) -> UserChatTarget | None:
        async with session_scope() as session:
            result = await session.execute(
                select(UserChatTarget).where(
                    UserChatTarget.user_id == user_id, UserChatTarget.telegram_chat_id == chat_id)
            )
            return result.scalar_one_or_none()

    async def get_chat(self, chat_id: int, *, user_id: int) -> TargetChat | None:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).join(
                    UserChatTarget, TargetChat.telegram_chat_id == UserChatTarget.telegram_chat_id
                ).where(
                    UserChatTarget.user_id == user_id,
                    TargetChat.telegram_chat_id == chat_id,
                )
            )
            return result.scalar_one_or_none()

    async def set_enabled(self, chat_id: int, enabled: bool, *, user_id: int) -> TargetChat | None:
        async with session_scope() as session:
            target = await session.execute(
                select(UserChatTarget).where(
                    UserChatTarget.user_id == user_id, UserChatTarget.telegram_chat_id == chat_id)
            )
            target = target.scalar_one_or_none()
            if target is None:
                return None
            target.enabled = enabled
            session.add(AuditEvent(
                operation="chat_set_enabled", status="ok",
                details={"chat_id": chat_id, "enabled": enabled, "user_id": user_id}))
            chat = await session.execute(select(TargetChat).where(TargetChat.telegram_chat_id == chat_id))
            return chat.scalar_one_or_none()
