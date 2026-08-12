"""Process entrypoints: bot+api (Railway service 1), worker (service 2), api-only."""

from __future__ import annotations

import asyncio
import logging
import time

from app.logging_conf import configure_logging

logger = logging.getLogger("app.main")

_HEARTBEAT_INTERVAL = 30


async def _bot_heartbeat_loop() -> None:
    from app.config import get_settings
    from app.services.status_service import HEARTBEAT_BOT_KEY

    settings = get_settings()
    while True:
        try:
            from redis.asyncio import from_url

            redis = from_url(settings.redis_url, decode_responses=True)
            try:
                await redis.set(HEARTBEAT_BOT_KEY, time.time(), ex=300)
            finally:
                await redis.aclose()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


async def _run_bot_service() -> None:
    from app.bot.bot_factory import build_dispatcher, start_bot_polling
    from app.config import get_settings
    from app.database.init_db import init_db
    from app.services.analysis_service import AnalysisService
    from app.services.chat_service import ChatService
    from app.telegram.session_store import SessionStore

    settings = get_settings()
    await init_db()

    # The bot process NEVER opens an MTProto session. The worker is the
    # single owner of the scanner account: it resolves chats, searches
    # history and runs analysis jobs. The bot only queues work and reads
    # worker-reported status from Redis.
    session_store = SessionStore()
    analysis = AnalysisService()
    chat_service = ChatService()
    dispatcher, bot = build_dispatcher(analysis, chat_service, session_store)
    if bot is None:
        raise RuntimeError("BOT_TOKEN is required to run the bot service")

    heartbeat_task = asyncio.create_task(_bot_heartbeat_loop())

    # API server + polling run in the same process (Railway service 1).
    from app.api.app import create_app

    api_app = create_app(analysis=analysis, chat_service=chat_service)

    async def _serve_api() -> None:
        import uvicorn

        config = uvicorn.Config(
            api_app, host=settings.api_host, port=settings.api_port, log_config=None
        )
        server = uvicorn.Server(config)
        await server.serve()

    try:
        await asyncio.gather(start_bot_polling(dispatcher, bot), _serve_api())
    finally:
        heartbeat_task.cancel()


def run_bot() -> None:
    from app.config import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_privacy_level)
    asyncio.run(_run_bot_service())


async def _run_worker_service() -> None:
    from app.database.init_db import init_db
    from app.workers.worker import build_worker

    await init_db()
    worker = build_worker()
    await worker.async_run()


def run_worker() -> None:
    from app.config import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_privacy_level)
    asyncio.run(_run_worker_service())


async def _run_api_service() -> None:
    from app.api.app import create_app
    from app.config import get_settings
    from app.database.init_db import init_db

    await init_db()
    api_app = create_app()
    import uvicorn

    settings = get_settings()
    config = uvicorn.Config(api_app, host=settings.api_host, port=settings.api_port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()


def run_api() -> None:
    from app.config import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_privacy_level)
    asyncio.run(_run_api_service())


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "bot"
    if command == "worker":
        run_worker()
    elif command == "api":
        run_api()
    else:
        run_bot()
