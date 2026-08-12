"""Shared handler helpers."""

from __future__ import annotations

from aiogram.types import Message


def chat_reference_from_message(message: Message) -> str | int | None:
    """Extract a chat reference: forwarded entity, else the command argument."""
    reply = message.reply_to_message
    if reply is not None and getattr(reply, "sender_chat", None) is not None:
        sender_chat = reply.sender_chat
        if sender_chat and sender_chat.id is not None and not getattr(reply, "from_user", None):
            return int(sender_chat.id)
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        return parts[1].strip()
    return None


def command_argument(message: Message) -> str | None:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        return parts[1].strip()
    return None
