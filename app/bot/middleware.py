"""Bot middlewares: authorization allowlist + structured logging.

Root admins (``ADMIN_USER_IDS``) and DB-authorized users may use the bot.
Public commands (``/start``, ``/help``) bypass authorization entirely.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from app.config import get_settings
from app.security.redact import mask_secret

logger = logging.getLogger("app.bot.middleware")

_UNAUTHORIZED_TEXT = "You are not authorized to use this bot. Please contact an administrator."

_PUBLIC_COMMANDS = {"/start", "/help", "start", "help"}


def _extract_command(event: Message | CallbackQuery) -> str | None:
    """Best-effort command extraction for auth-whitelist."""
    if isinstance(event, Message) and event.text:
        parts = event.text.strip().split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@")[0].casefold()
        return cmd
    if isinstance(event, CallbackQuery) and event.data:
        return event.data.split(":")[0] if ":" in event.data else event.data
    return None


def _extract_user_id(event: Message | CallbackQuery) -> int | None:
    if event.from_user:
        return event.from_user.id
    return None


def is_root_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    settings = get_settings()
    allowed = set(settings.admin_user_ids)
    return bool(allowed) and user_id in allowed


# Backward-compatible alias (used by security tests).  For new code prefer
# ``is_root_admin`` or the ``AuthorizationMiddleware``.
user_authorized = is_root_admin


class AuthorizationMiddleware(BaseMiddleware):
    """Gating middleware: public commands pass through; everything else requires
    authorisation (root admin or DB-authorized user)."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user_id = _extract_user_id(event) if isinstance(event, (Message, CallbackQuery)) else None
        command = _extract_command(event) if isinstance(event, (Message, CallbackQuery)) else None

        # Public commands always allowed.
        if command and command in _PUBLIC_COMMANDS:
            return await handler(event, data)

        # Check authorization (root admin OR DB-authorized).
        if user_id is None:
            if isinstance(event, (Message, CallbackQuery)):
                return
            return await handler(event, data)

        if is_root_admin(user_id):
            return await handler(event, data)

        # Lazy-import to avoid circular imports at module level.
        from app.services.authorization import is_user_authorized as _db_authorized  # noqa: PLC0415

        if await _db_authorized(user_id):
            return await handler(event, data)

        # Unauthorized.
        logger.warning(
            "bot: unauthorized attempt",
            extra={"extra": {"user_id": user_id, "operation": "unauthorized_attempt"}},
        )
        if isinstance(event, Message):
            await event.answer(_UNAUTHORIZED_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer(_UNAUTHORIZED_TEXT, show_alert=False)
        return


class OperationLogMiddleware(BaseMiddleware):
    """Logs operations with hashed user ids and durations — never content."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        started = time.monotonic()
        try:
            result = await handler(event, data)
            status = "ok"
        except Exception as exc:  # noqa: BLE001
            status = type(exc).__name__
            raise
        finally:
            user_id = None
            operation = "unknown"
            if isinstance(event, Message):
                user_id = event.from_user.id if event.from_user else None
                operation = event.text or (event.callback_query and "callback") or "message"
                if operation and len(operation) > 80:
                    operation = operation[:80]
            elif isinstance(event, CallbackQuery):
                user_id = event.from_user.id if event.from_user else None
                operation = f"callback:{event.data}"
            logger.info(
                "bot: update handled",
                extra={
                    "extra": {
                        "user_id": user_id,
                        "operation": mask_secret(str(operation), 40),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "status": status,
                    }
                },
            )
        return result
