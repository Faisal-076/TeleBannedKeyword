"""Job queueing with graceful degradation.

Primary path: arq (Redis). If Redis is unavailable, jobs run inline as
background asyncio tasks so the bot keeps working in degraded mode.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings

logger = logging.getLogger("app.services.queue")

_pool = None
_pool_task: asyncio.Task | None = None


def _bot_api_redis_settings() -> RedisSettings:
    """Bot/API pool: fail fast when Redis is down.

    The worker builds its own pool with retries (it must survive Redis
    coming up late); the bot/API processes must degrade quickly — every
    `enqueue`/`redis_available` call would otherwise stall ~6s on arq's
    default 5 retries.
    """
    return dataclasses.replace(
        RedisSettings.from_dsn(get_settings().redis_url),
        conn_retries=0,
        conn_timeout=1,
    )


async def _get_pool():
    global _pool, _pool_task
    if _pool is None:
        redis_settings = _bot_api_redis_settings()
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
