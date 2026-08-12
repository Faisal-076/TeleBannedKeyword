"""Dispatcher assembly: storage, middlewares, routers, workflow data."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import check, chats, commands
from app.bot.middleware import AuthorizationMiddleware, OperationLogMiddleware
from app.config import get_settings
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.telegram.gateway import TelegramGateway
from app.telegram.session_store import SessionStore

logger = logging.getLogger("app.bot.factory")


def build_dispatcher(
    gateway: TelegramGateway,
    analysis: AnalysisService,
    chat_service: ChatService,
    session_store: SessionStore,
) -> tuple[Dispatcher, Bot | None]:
    settings = get_settings()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["gateway"] = gateway
    dispatcher["analysis"] = analysis
    dispatcher["chats"] = chat_service
    dispatcher["session_store"] = session_store
    dispatcher["config"] = settings

    dispatcher.update.outer_middleware(AuthorizationMiddleware())
    dispatcher.update.outer_middleware(OperationLogMiddleware())

    dispatcher.include_router(commands.router)
    dispatcher.include_router(chats.router)
    dispatcher.include_router(check.router)

    if not settings.bot_configured:
        return dispatcher, None
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return dispatcher, bot


async def start_bot_polling(
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    logger.info("bot: starting polling")
    try:
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
