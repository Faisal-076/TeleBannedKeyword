"""Target chat management backed by the MTProto gateway.

Additions only succeed when the scanner account can actually reach the chat;
inaccessible private groups are rejected gracefully with a clear reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import AuditEvent, TargetChat
from app.security.redact import mask_username
from app.telegram.errors import AccessState
from app.telegram.gateway import TelegramGateway
from app.utils.timeutil import utc_now_naive

logger = logging.getLogger("app.services.chats")


@dataclass
class AddChatResult:
    ok: bool
    chat: TargetChat | None = None
    error: str | None = None
    access_state: str | None = None


class ChatService:
    def __init__(self, gateway: TelegramGateway) -> None:
        self._gateway = gateway

    async def add_chat(self, raw: str | int, actor: str = "cli") -> AddChatResult:
        resolved = await self._gateway.resolve_chat(raw)
        if resolved.access_state != AccessState.ACCESSIBLE:
            message = {
                AccessState.USERNAME_NOT_FOUND.value: "username not found",
                AccessState.PRIVATE_NO_ACCESS.value: (
                    "private chat — the scanner account is not a member"
                ),
                AccessState.NOT_MEMBER.value: "scanner account is not a member",
                AccessState.BANNED.value: "scanner account is banned in this chat",
                AccessState.RESTRICTED.value: "scanner account is restricted",
                AccessState.DELETED.value: "chat no longer exists",
                AccessState.MIGRATED.value: "chat was migrated; resolve the new chat",
                AccessState.INSUFFICIENT_PERMISSIONS.value: "insufficient permissions",
                AccessState.INVALID_INVITE.value: "invite link invalid or expired",
            }.get(resolved.error or "", resolved.error or "unreachable")
            return AddChatResult(ok=False, error=message, access_state=resolved.error)

        async with session_scope() as session:
            existing = await session.execute(
                select(TargetChat).where(TargetChat.telegram_chat_id == resolved.chat_id)
            )
            chat = existing.scalar_one_or_none()
            if chat is None:
                chat = TargetChat(
                    telegram_chat_id=resolved.chat_id,
                    title=resolved.title,
                    username=resolved.username,
                    chat_type=resolved.chat_type,
                    access_state=AccessState.ACCESSIBLE.value,
                    last_verified_at=utc_now_naive(),
                )
                session.add(chat)
            else:
                chat.title = resolved.title
                chat.username = resolved.username
                chat.chat_type = resolved.chat_type
                chat.access_state = AccessState.ACCESSIBLE.value
                chat.access_error = None
                chat.last_verified_at = utc_now_naive()
            await session.flush()
            session.add(
                AuditEvent(
                    user_id_hash=actor,
                    operation="chat_add",
                    status="ok",
                    details={"chat_id": resolved.chat_id},
                )
            )
            return AddChatResult(ok=True, chat=chat)

    async def remove_chat(self, chat_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).where(TargetChat.telegram_chat_id == chat_id)
            )
            chat = result.scalar_one_or_none()
            if chat is None:
                return False
            await session.delete(chat)
            session.add(
                AuditEvent(operation="chat_remove", status="ok", details={"chat_id": chat_id})
            )
            return True

    async def list_chats(self) -> list[TargetChat]:
        async with session_scope() as session:
            result = await session.execute(select(TargetChat).order_by(TargetChat.id))
            return list(result.scalars().all())

    async def get_chat(self, chat_id: int) -> TargetChat | None:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).where(TargetChat.telegram_chat_id == chat_id)
            )
            return result.scalar_one_or_none()

    async def set_enabled(self, chat_id: int, enabled: bool) -> TargetChat | None:
        async with session_scope() as session:
            result = await session.execute(
                select(TargetChat).where(TargetChat.telegram_chat_id == chat_id)
            )
            chat = result.scalar_one_or_none()
            if chat is None:
                return None
            chat.enabled = enabled
            session.add(
                AuditEvent(
                    operation="chat_set_enabled",
                    status="ok",
                    details={"chat_id": chat_id, "enabled": enabled},
                )
            )
            return chat

    async def check_chat(self, chat: TargetChat) -> dict:
        """Re-verify access and refresh metadata. Never raises for one chat."""
        try:
            info = await self._gateway.get_chat_info(chat.telegram_chat_id)
        except Exception as exc:  # noqa: BLE001
            return {"chat_id": chat.telegram_chat_id, "ok": False, "error": type(exc).__name__}
        ok = info.access_state == AccessState.ACCESSIBLE
        async with session_scope() as session:
            row = await session.get(TargetChat, chat.id)
            if row is not None:
                row.access_state = info.access_state.value
                row.access_error = None if ok else info.error
                row.last_verified_at = utc_now_naive()
                if info.title:
                    row.title = info.title
        return {
            "chat_id": chat.telegram_chat_id,
            "title": info.title or chat.title,
            "username": mask_username(info.username or chat.username),
            "access_state": info.access_state.value,
            "ok": ok,
        }
