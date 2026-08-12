"""System status collection for /health, /ready, /status, /admin status.

Never exposes secrets. Includes worker heartbeat age and analysis stats.

MTProto connection state is reported by the worker (the single MTProto
owner) into Redis (`tbk:mtproto:state`); the bot and API processes only
read that key — they never open their own MTProto session.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.config import get_settings
from app.database.engine import check_database, session_scope
from app.database.models import AnalysisRequest
from app.services.queue import redis_available
from app.telegram.gateway import TelegramGateway

logger = logging.getLogger("app.services.status")

HEARTBEAT_BOT_KEY = "tbk:heartbeat:bot"
HEARTBEAT_WORKER_KEY = "tbk:heartbeat:worker"
MT_PROTO_STATE_KEY = "tbk:mtproto:state"


def _heartbeat_age(redis, key: str, max_age: int = 120) -> float | None:
    """Return age in seconds of a heartbeat key, or None if absent."""
    try:
        raw = redis.get(key)
        if not raw:
            return None
        ts = float(raw)
        return time.time() - ts
    except Exception:  # noqa: BLE001
        return None


async def set_mtproto_state(
    redis, connected: bool, last_connected: datetime | None, username: str | None = None
) -> None:
    """Worker-only: publish MTProto connection state for other processes."""
    await redis.set(
        MT_PROTO_STATE_KEY,
        json.dumps(
            {
                "connected": bool(connected),
                "last_connected": last_connected.isoformat() if last_connected else None,
                "username": username,
                "reported_at": time.time(),
            }
        ),
        ex=300,
    )


async def get_mtproto_state() -> dict:
    """Read the worker-reported MTProto state (bot/API processes)."""
    empty = {
        "connected": False,
        "last_connected": None,
        "username": None,
        "reported_at": None,
        "available": False,
    }
    settings = get_settings()
    if not await redis_available():
        return empty
    from redis.asyncio import from_url

    redis = from_url(settings.redis_url, decode_responses=True)
    try:
        raw = await redis.get(MT_PROTO_STATE_KEY)
    finally:
        await redis.aclose()
    if not raw:
        return {**empty, "available": True}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return {**empty, "available": True}
    state["available"] = True
    return state


async def collect_status(
    gateway: TelegramGateway | None,
    bot_connected: bool | None = None,
    *,
    include_secrets: bool = False,
) -> dict:
    settings = get_settings()
    redis_ok = await redis_available()
    if gateway is not None:
        mtproto = {
            "connected": bool(gateway.connected),
            "configured": settings.mtproto_configured,
            "session_present": settings.session_configured,
            "last_connected": gateway.last_connected.isoformat()
            if gateway.last_connected
            else None,
        }
    else:
        state = await get_mtproto_state()
        mtproto = {
            "connected": state.get("connected", False),
            "configured": settings.mtproto_configured,
            "session_present": settings.session_configured,
            "last_connected": state.get("last_connected"),
        }
    status: dict = {
        "service": settings.environment,
        "database": "ok" if await check_database() else "error",
        "redis": "ok" if redis_ok else "error",
        "mtproto": mtproto,
        "bot_api": {
            "configured": settings.bot_configured,
            "connected": bool(bot_connected) if bot_connected is not None else None,
        },
        "worker_heartbeat_age": None,
        "bot_heartbeat_age": None,
        "analysis": {"queued": 0, "running": 0, "failed": 0, "total": 0},
    }
    if redis_ok:
        from redis.asyncio import from_url

        redis = from_url(settings.redis_url, decode_responses=True)
        try:
            status["worker_heartbeat_age"] = _heartbeat_age(redis, HEARTBEAT_WORKER_KEY)
            status["bot_heartbeat_age"] = _heartbeat_age(redis, HEARTBEAT_BOT_KEY)
        finally:
            await redis.aclose()
    try:
        async with session_scope() as session:
            rows = await session.execute(
                select(AnalysisRequest.status, func.count(AnalysisRequest.id))
                .group_by(AnalysisRequest.status)
            )
            counts = {key: 0 for key in ("queued", "running", "failed")}
            for state, count in rows.all():
                if state in counts:
                    counts[state] = count
            total = await session.execute(select(func.count(AnalysisRequest.id)))
            status["analysis"].update(counts)
            status["analysis"]["total"] = total.scalar() or 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("status: analysis stats unavailable: %s", type(exc).__name__)
    return status
