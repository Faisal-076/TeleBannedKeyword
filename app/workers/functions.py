"""arq worker functions.

Each job constructs its dependencies lazily and is idempotent. A failure in
one job (e.g. one target chat) never crashes the worker.

The worker process is the SINGLE owner of the MTProto session: chat
resolution, history search, analysis and session revocation all happen
here — never in the bot or API processes.
"""

from __future__ import annotations

import logging
import time

from arq.connections import ArqRedis

from app.services.status_service import HEARTBEAT_WORKER_KEY, set_mtproto_state

logger = logging.getLogger("app.workers")

# Per-process singletons (one worker process = one gateway connection).
_gateway = None
_sync_service = None
_analysis_service = None
_chat_service = None
_session_store = None

# Cached at startup so the heartbeat (every 30 s) never re-reads the
# session file.  These are just the static worker-environment facts;
# they never change across the worker's lifetime.
_mtproto_configured: bool | None = None
_session_present: bool | None = None


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


def _get_chat_service():
    global _chat_service
    if _chat_service is None:
        from app.services.chat_service import ChatService

        _chat_service = ChatService()
    return _chat_service


def _get_session_store():
    global _session_store
    if _session_store is None:
        from app.telegram.session_store import SessionStore

        _session_store = SessionStore()
    return _session_store


async def startup(ctx) -> None:
    global _mtproto_configured, _session_present

    from app.config import get_settings

    settings = get_settings()
    _mtproto_configured = settings.mtproto_configured
    _session_present = (await _get_session_store().load()) is not None
    logger.info("worker: starting up (configured=%s session=%s)", _mtproto_configured, _session_present)
    gateway = _get_gateway()
    connected = await gateway.connect()
    await _report_mtproto_state(connected)


async def shutdown(ctx) -> None:
    logger.info("worker: shutting down")
    gateway = _get_gateway()
    if gateway.connected:
        await gateway.disconnect()
    await _report_mtproto_state(False)


async def _report_mtproto_state(connected: bool) -> None:
    """Publish MTProto state to Redis for the bot/API status endpoints.

    configured/session_present come from the WORKER's environment and
    session store — the only process that owns them.  Values are cached
    at startup so heartbeat calls never re-read the session file.
    """
    global _mtproto_configured, _session_present

    try:
        from app.services.queue import redis_available

        if not await redis_available():
            return
        from app.config import get_settings
        from app.services.redis_client import redis_from_url

        settings = get_settings()
        redis = redis_from_url(settings.redis_url, decode_responses=True)
        try:
            gateway = _get_gateway()
            await set_mtproto_state(
                redis,
                connected,
                gateway.last_connected,
                gateway.account_username,
                configured=_mtproto_configured,
                session_present=_session_present,
            )
        finally:
            await redis.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.debug("worker: mtproto state report failed: %s", type(exc).__name__)


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


async def recover_queued(ctx: dict) -> dict:
    """Re-enqueue analysis requests stuck QUEUED after a Redis outage."""
    recovered = await _get_analysis_service().recover_queued()
    if recovered:
        logger.info("worker: recovered %d queued analysis jobs", recovered)
    return {"recovered": recovered}


async def add_chat(ctx: dict, reference: str, actor: str) -> dict:
    """Resolve + persist a chat via MTProto (worker is the MTProto owner)."""
    from app.telegram.errors import AccessState

    resolved = await _get_gateway().resolve_chat(reference)
    if resolved.access_state != AccessState.ACCESSIBLE:
        error = {
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
        await _notify_user(actor, f"❌ Cannot add chat: {error}")
        return {"ok": False, "error": resolved.error}

    result = await _get_chat_service().persist_resolved(resolved, actor=actor)
    if not result.ok or result.chat is None:
        await _notify_user(actor, f"❌ Cannot add chat: {result.error}")
        return {"ok": False, "error": result.error}
    chat = result.chat
    username = f"@{chat.username}" if chat.username else "-"
    await _notify_user(
        actor,
        "✅ Chat added (verified by the worker)\n"
        f"Title: {chat.title or '-'}\n"
        f"Username: {username}\n"
        f"Type: {chat.chat_type}\n"
        f"Chat id: {chat.telegram_chat_id}\n"
        f"Enabled: {chat.enabled}\n\n"
        f"Run /sync {chat.telegram_chat_id} initial to index its history.",
    )
    return {"ok": True, "chat_id": chat.telegram_chat_id}


async def check_chat(ctx: dict, chat_id: int, actor: str) -> dict:
    """Re-verify access to a chat via MTProto and report the result."""
    from app.bot.handlers.chats import _ACCESS_LABEL

    chat = await _get_chat_service().get_chat(chat_id)
    if chat is None:
        await _notify_user(actor, "Chat not found.")
        return {"chat_id": chat_id, "status": "not_found"}
    info = await _get_gateway().get_chat_info(chat_id)
    updated = await _get_chat_service().apply_chat_info(chat, info)
    state = updated.get("access_state", "error")
    label = _ACCESS_LABEL.get(state, state)
    await _notify_user(
        actor,
        f"🔎 {updated.get('title', chat.title or '')}\n"
        f"Username: {updated.get('username', '-')}\n"
        f"Chat id: {chat.telegram_chat_id}\n"
        f"Access: {label}\n"
        f"Type: {chat.chat_type}\n"
        f"Sync: {chat.sync_state} (indexed {chat.sync_indexed_count})",
    )
    return {"chat_id": chat_id, "ok": updated["ok"], "access_state": state}


async def revoke_session(ctx: dict) -> dict:
    """Revoke the scanner session: disconnect + mark revoked + wipe file."""
    gateway = _get_gateway()
    if gateway.connected:
        await gateway.disconnect()
    await _get_session_store().revoke()
    await _report_mtproto_state(False)
    logger.warning("worker: session revoked via job")
    return {"revoked": True}


async def heartbeat(ctx: dict) -> None:
    redis: ArqRedis = ctx["redis"]
    await redis.set(HEARTBEAT_WORKER_KEY, time.time(), ex=300)
    gateway = _get_gateway()
    await set_mtproto_state(
        redis,
        gateway.connected,
        gateway.last_connected,
        gateway.account_username,
        configured=_mtproto_configured,
        session_present=_session_present,
    )
    logger.debug("worker: heartbeat")


async def _notify_user(actor: str, text: str) -> None:
    """Send a message to a Telegram user via Bot API (worker-side)."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.bot_configured:
        return
    if not actor or actor == "api" or not actor.lstrip("-").isdigit():
        return
    try:
        from aiogram import Bot

        bot = Bot(token=settings.bot_token.get_secret_value())
        try:
            await bot.send_message(int(actor), text)
        finally:
            await bot.session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "worker: notification failed user=%s err=%s",
            actor,
            type(exc).__name__,
        )


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
