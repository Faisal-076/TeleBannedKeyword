"""Job queueing with graceful degradation.

Primary path: arq (Redis). If Redis is unavailable, jobs run inline as
background asyncio tasks so the bot keeps working in degraded mode.
"""

from __future__ import annotations

import asyncio
import logging

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings

logger = logging.getLogger("app.services.queue")

_pool = None
_pool_task: asyncio.Task | None = None


async def _get_pool():
    global _pool, _pool_task
    if _pool is None:
        settings = get_settings()
        redis_settings = RedisSettings.from_dsn(settings.redis_url)
        if _pool_task is None:
            async def _connect():
                global _pool
                try:
                    _pool = await create_pool(redis_settings)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("queue: redis unavailable: %s", type(exc).__name__)
                    _pool = None

            _pool_task = asyncio.create_task(_connect())
        await _pool_task
        _pool_task = None
    return _pool


async def enqueue(function_name: str, *args, job_id: str | None = None) -> bool:
    """Enqueue a job; returns False when Redis is down (caller decides fallback)."""
    pool = await _get_pool()
    if pool is None:
        return False
    try:
        kwargs = {"_job_id": job_id} if job_id else {}
        await pool.enqueue_job(function_name, *args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue: enqueue failed: %s", type(exc).__name__)
        return False


async def redis_available() -> bool:
    pool = await _get_pool()
    return pool is not None
