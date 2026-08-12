"""Bot middlewares: authorization allowlist + structured logging."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from app.config import get_settings
from app.security.redact import mask_secret

logger = logging.getLogger("app.bot.middleware")

_UNAUTHORIZED_TEXT = "Unauthorized."


def user_authorized(user_id: int | None) -> bool:
    if user_id is None:
        return False
    settings = get_settings()
    allowed = set(settings.admin_user_ids)
    return bool(allowed) and user_id in allowed


class AuthorizationMiddleware(BaseMiddleware):
    """Only ADMIN_USER_IDS may interact with the bot."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
        if not user_authorized(user_id):
            if user_id is None:
                return
            logger.warning(
                "bot: unauthorized attempt",
                extra={"extra": {"user_id": user_id, "operation": "unauthorized_attempt"}},
            )
            if isinstance(event, Message):
                await event.answer(_UNAUTHORIZED_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(_UNAUTHORIZED_TEXT, show_alert=False)
            return
        return await handler(event, data)


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
