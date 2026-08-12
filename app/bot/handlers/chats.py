"""Target chat management commands — all user-scoped."""

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


def _caller_id(message: Message) -> int:
    return message.from_user.id if message.from_user else 0


@router.message(Command("addchat"))
async def cmd_addchat(message: Message, command: CommandObject) -> None:
    reference = chat_reference_from_message(message) or (command.args or "").strip()
    if not reference:
        await message.answer("Usage: /addchat <@username | t.me link | invite link | chat id>")
        return
    user_id = _caller_id(message)
    queued = await enqueue("add_chat", reference, str(user_id), job_id=f"add-chat:{user_id}:{reference}")
    if queued:
        await message.answer("Chat verification queued. The worker will report the result here.")
    else:
        await message.answer("Cannot queue (Redis unavailable).")


@router.message(Command("removechat"))
async def cmd_removechat(message: Message, command: CommandObject, chats: ChatService) -> None:
    user_id = _caller_id(message)
    reference = chat_reference_from_message(message) or (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats, user_id)
    if chat_id is None:
        await message.answer("Chat is not configured for your account.")
        return
    if await chats.remove_chat(chat_id, user_id=user_id):
        await message.answer(f"Removed chat {chat_id}.")
    else:
        await message.answer("Chat is not configured for your account.")


@router.message(Command("listchats"))
async def cmd_listchats(message: Message, chats: ChatService) -> None:
    user_id = _caller_id(message)
    chat_list = await chats.list_chats(user_id)
    if not chat_list:
        await message.answer("You have no configured chats. Use /addchat.")
        return
    lines = ["Your configured chats"]
    for chat in chat_list:
        username = f"@{chat.username}" if chat.username else "-"
        enabled = "enabled" if chat.enabled else "disabled"
        lines.append(f"{chat.title or '(untitled)'} | {username} | {chat.chat_type} | {chat.access_state} | {enabled} | {chat.telegram_chat_id}")
    await message.answer("\n".join(lines))


@router.message(Command("chatinfo"))
async def cmd_chatinfo(message: Message, command: CommandObject, chats: ChatService) -> None:
    user_id = _caller_id(message)
    reference = chat_reference_from_message(message) or (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats, user_id)
    if chat_id is None:
        await message.answer("Chat is not configured for your account.")
        return
    queued = await enqueue("check_chat", chat_id, str(user_id), job_id=f"check-chat:{user_id}:{chat_id}")
    if queued:
        await message.answer("Access re-verification queued. Result will appear here.")
    else:
        await message.answer("Cannot queue (Redis unavailable).")


@router.message(Command("enablechat"))
async def cmd_enablechat(message: Message, command: CommandObject, chats: ChatService) -> None:
    user_id = _caller_id(message)
    reference = (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats, user_id)
    if chat_id is None:
        await message.answer("Chat is not configured for your account.")
        return
    chat = await chats.set_enabled(chat_id, True, user_id=user_id)
    await message.answer(f"Enabled chat {chat_id}." if chat else "Chat is not configured for your account.")


@router.message(Command("disablechat"))
async def cmd_disablechat(message: Message, command: CommandObject, chats: ChatService) -> None:
    user_id = _caller_id(message)
    reference = (command.args or "").strip()
    chat_id = await _resolve_to_id(reference, chats, user_id)
    if chat_id is None:
        await message.answer("Chat is not configured for your account.")
        return
    chat = await chats.set_enabled(chat_id, False, user_id=user_id)
    await message.answer(f"Disabled chat {chat_id}." if chat else "Chat is not configured for your account.")


async def _resolve_to_id(reference: str, chats: ChatService, user_id: int) -> int | None:
    if not reference:
        return None
    ref = reference.strip().lstrip("@")
    chat_list = await chats.list_chats(user_id)
    if ref.isdigit():
        cid = int(ref)
        if any(c.telegram_chat_id == cid or c.id == cid for c in chat_list):
            return cid
    for chat in chat_list:
        if chat.username and chat.username.lower() == ref.lower():
            return chat.telegram_chat_id
        if str(chat.telegram_chat_id) == ref:
            return chat.telegram_chat_id
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
