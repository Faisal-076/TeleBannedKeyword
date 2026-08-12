"""Target chat management commands: /addchat /removechat /listchats
/chatinfo /enablechat /disablechat.

Chat verification (which requires MTProto) is queued to the worker — the
bot process never opens an MTProto session. Results are reported back via
the worker's Bot API notifications.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.helpers import chat_reference_from_message
from app.services.chat_service import ChatService
from app.services.queue import enqueue
from app.telegram.errors import AccessState

logger = logging.getLogger("app.bot.handlers.chats")

router = Router(name="chats")

_ACCESS_LABEL = {
    AccessState.ACCESSIBLE.value: "accessible",
    AccessState.NOT_MEMBER.value: "not a member",
    AccessState.BANNED.value: "banned",
    AccessState.RESTRICTED.value: "restricted",
    AccessState.PRIVATE_NO_ACCESS.value: "private — no access",
    AccessState.DELETED.value: "deleted",
    AccessState.MIGRATED.value: "migrated",
    AccessState.USERNAME_NOT_FOUND.value: "username not found",
    AccessState.INSUFFICIENT_PERMISSIONS.value: "insufficient permissions",
    AccessState.NOT_VERIFIED.value: "not verified",
    AccessState.ERROR.value: "error",
}


@router.message(Command("addchat"))
async def cmd_addchat(message: Message, command: CommandObject) -> None:
    reference = chat_reference_from_message(message) or (command.args or "").strip()
    if not reference:
        await message.answer(
            "Usage: /addchat <@username | t.me link | invite link | chat id>\n"
            "You can also reply to a forwarded message from the chat."
        )
        return
    queued = await enqueue(
        "add_chat", reference, str(message.from_user.id),
        job_id=f"add-chat:{reference}",
    )
    if queued:
        await message.answer(
            "🔍 Chat verification queued. The worker will resolve access "
            "via the scanner account and report the result here."
        )
    else:
        await message.answer(
            "❌ Cannot queue chat verification (Redis unavailable). "
            "Retry when the worker is online."
        )


@router.message(Command("removechat"))
async def cmd_removechat(message: Message, command: CommandObject, chats: ChatService) -> None:
    reference = chat_reference_from_message(message) or (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats)
    if chat_id is None:
        await message.answer("Chat not found.")
        return
    if await chats.remove_chat(chat_id):
        await message.answer(f"Removed chat {chat_id}.")
    else:
        await message.answer("Chat not found.")


@router.message(Command("listchats"))
async def cmd_listchats(message: Message, chats: ChatService) -> None:
    chat_list = await chats.list_chats()
    if not chat_list:
        await message.answer("No chats configured. Use /addchat.")
        return
    lines = ["💬 Configured chats"]
    for chat in chat_list:
        username = f"@{chat.username}" if chat.username else "-"
        enabled = "enabled" if chat.enabled else "disabled"
        lines.append(
            f"• {chat.title or '(untitled)'} | {username} | {chat.chat_type} | "
            f"{chat.access_state} | {enabled} | {chat.telegram_chat_id}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("chatinfo"))
async def cmd_chatinfo(message: Message, command: CommandObject, chats: ChatService) -> None:
    reference = chat_reference_from_message(message) or (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats)
    if chat_id is None:
        await message.answer("Chat not found.")
        return
    queued = await enqueue(
        "check_chat", chat_id, str(message.from_user.id),
        job_id=f"check-chat:{chat_id}",
    )
    if queued:
        await message.answer(
            "🔎 Access re-verification queued. The result will be reported here."
        )
    else:
        await message.answer(
            "❌ Cannot queue verification (Redis unavailable). Retry when the worker is online."
        )


@router.message(Command("enablechat"))
async def cmd_enablechat(message: Message, command: CommandObject, chats: ChatService) -> None:
    reference = (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats)
    if chat_id is None:
        await message.answer("Chat not found.")
        return
    chat = await chats.set_enabled(chat_id, True)
    await message.answer(f"Enabled chat {chat_id}." if chat else "Chat not found.")


@router.message(Command("disablechat"))
async def cmd_disablechat(message: Message, command: CommandObject, chats: ChatService) -> None:
    reference = (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats)
    if chat_id is None:
        await message.answer("Chat not found.")
        return
    chat = await chats.set_enabled(chat_id, False)
    await message.answer(f"Disabled chat {chat_id}." if chat else "Chat not found.")


async def _resolve_to_id(reference: str, chats: ChatService) -> int | None:
    if not reference:
        return None
    ref = reference.strip().lstrip("@")
    chat_list = await chats.list_chats()
    if ref.isdigit():
        chat_id = int(ref)
        if any(c.telegram_chat_id == chat_id or c.id == chat_id for c in chat_list):
            return chat_id
    for chat in chat_list:
        if chat.username and chat.username.lower() == ref.lower():
            return chat.telegram_chat_id
        if str(chat.telegram_chat_id) == ref:
            return chat.telegram_chat_id
    return None
