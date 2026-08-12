"""arq worker functions.

Each job constructs its dependencies lazily and is idempotent. A failure in
one job (e.g. one target chat) never crashes the worker.
"""

from __future__ import annotations

import logging
import time

from arq.connections import ArqRedis

from app.services.status_service import HEARTBEAT_WORKER_KEY

logger = logging.getLogger("app.workers")

# Per-process singletons (one worker process = one gateway connection).
_gateway = None
_sync_service = None
_analysis_service = None


def _get_gateway():
    global _gateway
    if _gateway is None:
        from app.telegram.gateway import create_gateway

        _gateway = create_gateway()
    return _gateway


def _get_analysis_service():
    global _analysis_service
    if _analysis_service is None:
        from app.services.analysis_service import AnalysisService

        _analysis_service = AnalysisService(_get_gateway())
    return _analysis_service


def _get_sync_service():
    global _sync_service
    if _sync_service is None:
        from app.services.sync_service import SyncService

        _sync_service = SyncService(_get_gateway())
    return _sync_service


async def startup(ctx) -> None:
    logger.info("worker: starting up")
    await _get_gateway().connect()


async def shutdown(ctx) -> None:
    logger.info("worker: shutting down")
    await _get_gateway().disconnect()


async def analyze_message(ctx: dict, request_id: str) -> dict:
    started = time.monotonic()
    outcome = await _get_analysis_service().run_request(request_id)
    duration_ms = int((time.monotonic() - started) * 1000)
    if outcome is None:
        return {"request_id": request_id, "status": "skipped"}
    await _notify_bot(request_id)
    logger.info(
        "worker: analyze_message done request=%s duration_ms=%d",
        request_id,
        duration_ms,
        extra={"extra": {"request_id": request_id, "duration_ms": duration_ms, "operation": "analyze_message"}},
    )
    return {"request_id": request_id, "status": "done", "score": outcome.overall_score}


async def sync_chat(ctx: dict, chat_id: int, mode: str = "incremental") -> dict:
    report = await _get_sync_service().sync_chat(chat_id, mode)
    if report is None:
        return {"chat_id": chat_id, "status": "not_found"}
    return {
        "chat_id": chat_id,
        "mode": mode,
        "processed": report.processed,
        "new_messages": report.new_messages,
        "end_reached": report.end_reached,
        "error": report.error,
    }


async def run_retention(ctx: dict) -> dict:
    from app.services.retention import purge_expired

    counts = await purge_expired()
    return counts


async def heartbeat(ctx: dict) -> None:
    redis: ArqRedis = ctx["redis"]
    await redis.set(HEARTBEAT_WORKER_KEY, time.time(), ex=300)
    logger.debug("worker: heartbeat")


async def _notify_bot(request_id: str) -> None:
    """Send the analysis result to the requesting user via Bot API."""
    from app.config import get_settings
    from app.database.engine import session_scope
    from app.database.models import AnalysisRequest

    settings = get_settings()
    if not settings.bot_configured:
        return
    service = _get_analysis_service()
    user_id = await service.get_request_user(request_id)
    outcome = await service.get_outcome(request_id)
    if user_id is None or outcome is None:
        return
    try:
        from aiogram import Bot
        from app.bot.formatters import format_result, build_result_keyboard

        bot = Bot(token=settings.bot_token.get_secret_value())
        try:
            text = format_result(outcome)
            await bot.send_message(
                user_id, text, reply_markup=build_result_keyboard(request_id)
            )
        finally:
            await bot.session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "worker: result notification failed user=%s err=%s",
            user_id,
            type(exc).__name__,
        )
